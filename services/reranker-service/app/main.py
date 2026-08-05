"""FR-25: cross-encoder reranking pass over top-N hybrid retrieval candidates.
The only ML-serving piece besides embeddings that's fully functional this
session -- small enough to finish now, and orchestration-mcp's hybrid search
(TODO) will call this once it exists."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import pyroscope
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from pydantic import BaseModel
from sentence_transformers import CrossEncoder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import metrics

# #73: honor LOG_LEVEL like the other services. Deliberately stdlib-only
# rather than common.logging_setup: this service has no dependency on
# services/common (no DB, no audit writes, so no SIEM hook either), and a
# log-format preference isn't worth adding one. A typo'd level falls back to
# INFO instead of crashing the pod, matching the shared module's behavior.
_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
logging.basicConfig(
    level=_level if _level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO",
    format="%(asctime)s %(levelname)s [reranker-service] %(name)s: %(message)s",
)


def _setup_tracing() -> None:
    """#134: minimal inline equivalent of common/tracing.py's setup (this
    service doesn't depend on services/common). Same standard env vars, same
    posture: disabled unless OTEL_EXPORTER_OTLP_ENDPOINT is set, ParentBased
    sampling so this service simply follows orchestration-mcp's per-request
    decision -- the incoming rerank span is what makes the cross-encoder's
    time visible instead of opaque (#134's stated goal for this hop)."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return
    try:
        ratio = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.05"))
    except ValueError:
        ratio = 0.05
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", "nexus-rag-reranker-service")}
        ),
        sampler=ParentBased(TraceIdRatioBased(min(max(ratio, 0.0), 1.0))),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def _setup_profiling() -> None:
    """#349: minimal inline equivalent of common/profiling.py's setup (this
    service doesn't depend on services/common). Same posture: disabled
    unless PYROSCOPE_SERVER_ADDRESS is set, continuous CPU-only sampling --
    this is the service where a slow cross-encoder batch is otherwise
    invisible between the rerank span's start and end timestamps."""
    server_address = os.environ.get("PYROSCOPE_SERVER_ADDRESS", "").strip()
    if not server_address:
        logging.getLogger("reranker-service").info(
            "profiling disabled (PYROSCOPE_SERVER_ADDRESS is not set)"
        )
        return
    try:
        rate = int(os.environ.get("PYROSCOPE_SAMPLE_RATE", "100"))
    except ValueError:
        rate = 100
    rate = rate if rate > 0 else 100
    pyroscope.configure(
        application_name=os.environ.get("PYROSCOPE_APPLICATION_NAME", "nexus-rag-reranker-service"),
        server_address=server_address,
        sample_rate=rate,
        cpu_enabled=True,
        mem_enabled=False,
    )
    logging.getLogger("reranker-service").info(
        "profiling enabled: pushing to %s, %sHz CPU sampling (#349)", server_address, rate
    )


_setup_tracing()
_setup_profiling()
tracer = trace.get_tracer("reranker-service")

MODEL_NAME = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L6-v2")
# #210: pin the revision so a silent upstream update to MODEL_NAME's weights
# can't change retrieval ranking with no signal, and so an air-gapped
# deployment mirroring this model internally has a fixed commit to mirror to.
# Only meaningful for the default above -- overriding RERANKER_MODEL to a
# different repo without also overriding this is a deliberate unpin, not a
# silent one.
MODEL_REVISION = os.environ.get(
    "RERANKER_MODEL_REVISION", "c5ee24cb16019beea0893ab7796b1df96625c6b8"
)

# Issue #393: the model's input window, stated here as a decision instead of
# inherited as a tokenizer default nobody chose. ms-marco-MiniLM-L6-v2 is a
# BERT with max_position_embeddings=512; override alongside RERANKER_MODEL
# when swapping in a longer-window model.
MAX_LENGTH = int(os.environ.get("RERANKER_MAX_LENGTH", "512"))

# Issue #393: measured on this stack's own corpus (1293 chunks, the repo's
# docs ingested as a dev corpus): 12% of chunks overflow the 512-token window
# paired with a typical query -- mean overflow 323 tokens, worst case 67% of
# the chunk -- and sentence-transformers truncates silently, so the reranker
# was ordering those chunks on their first ~two-thirds. With window scoring
# on, an oversized chunk is scored as the max over overlapping windows that
# each fit the model, so a relevant passage in the chunk's tail counts.
# Cost is bounded by the same measurement: only the oversized 12% grow into
# 2-4 pairs each. Set to "false" to restore plain head-truncation scoring
# (which is still counted in the oversized-chunks metric either way).
WINDOW_SCORING = os.environ.get("RERANKER_WINDOW_SCORING", "true").strip().lower() != "false"
# Fraction of each window re-covered by the next, so a passage straddling a
# window boundary is seen whole by at least one window.
_WINDOW_OVERLAP = 0.25

# Hard ceiling on one /rerank call's batch. _model.predict() scores every
# (query, chunk) pair synchronously on CPU against a single shared
# CrossEncoder, so an oversized batch doesn't just make that one call slow --
# it holds the model for every concurrent caller. orchestration-mcp's own
# MAX_TOP_K keeps its requests to ~200 chunks (top_k * HYBRID_CANDIDATE_
# MULTIPLIER), so this leaves headroom for that path while still refusing a
# batch no legitimate caller produces. Rejecting rather than truncating is
# deliberate: a silently shortened batch would degrade ranking quality with
# no signal, whereas orchestration-mcp already handles a failed rerank by
# falling back to fused order *and saying so* in its response note.
MAX_RERANK_CHUNKS = int(os.environ.get("MAX_RERANK_CHUNKS", "512"))

# Issue #216: /rerank otherwise has no authorization model of its own --
# reachability is authorization, and the chart's NetworkPolicy restricting who
# can reach it (#131) is inert on a CNI that doesn't enforce policy. A shared
# secret means that gap alone no longer hands an attacker the post-access-
# filter chunk text (classified content) this service receives. Deliberately
# not the caller's OIDC token: FR-26 enforcement already happened in
# orchestration-mcp before this hop, and re-verifying it here would duplicate
# that logic in a second place it can drift out of sync (see the issue's own
# discussion of that heavier option). Empty by default so the local dev stack
# and any environment that hasn't set it up yet keep working unauthenticated,
# same posture as today -- but loudly, not silently (see the warning below).
RERANKER_SHARED_SECRET = os.environ.get("RERANKER_SHARED_SECRET", "")

# Issue #281 gap G5 stage 2: a second, optional value accepted alongside the
# primary during rotation, so orchestration-mcp can be switched to a new
# secret and this service restarted with it without a window where every
# request 401s until both sides agree. Set only while a rotation is in
# progress -- see docs/credential-rotation.md's reranker section for the
# order of operations -- and unset once orchestration-mcp is confirmed on
# the new value.
RERANKER_SHARED_SECRET_PREVIOUS = os.environ.get("RERANKER_SHARED_SECRET_PREVIOUS", "")

if not RERANKER_SHARED_SECRET:  # pragma: no cover - startup-time side effect
    logging.getLogger("reranker-service").warning(
        "RERANKER_SHARED_SECRET is not set -- /rerank accepts any caller that can "
        "reach this service on the network, with no credential of its own "
        "(issue #216). Acceptable for local dev; set this in any deployment where "
        "the NetworkPolicy might not be the only thing standing between an "
        "unauthorized caller and retrieved chunk content."
    )


class _SecurityHeadersMiddleware:
    """Issues #444/#445: X-Content-Type-Options + Referrer-Policy on every
    response. Minimal inline equivalent of
    common/security_headers.py's SecurityHeadersMiddleware -- this service
    doesn't depend on services/common (see _setup_tracing/_setup_profiling
    above for the same split, and services/common/common/security_headers.py's
    docstring for why this can't just be imported)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


_model: CrossEncoder | None = None


def _check_shared_secret(
    x_reranker_shared_secret: Annotated[str | None, Header()] = None,
) -> None:
    if not RERANKER_SHARED_SECRET:
        return
    presented = x_reranker_shared_secret or ""
    valid = hmac.compare_digest(presented, RERANKER_SHARED_SECRET) or (
        bool(RERANKER_SHARED_SECRET_PREVIOUS)
        and hmac.compare_digest(presented, RERANKER_SHARED_SECRET_PREVIOUS)
    )
    if not x_reranker_shared_secret or not valid:
        metrics.requests_total.labels(outcome="unauthorized").inc()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing or invalid X-Reranker-Shared-Secret"
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # The module-level singleton is deliberate: the model is loaded once at
    # startup and read by both /health and /rerank, neither of which takes a
    # Request, so app.state isn't reachable from them without changing both
    # signatures. One process, one model, assigned exactly here.
    global _model  # noqa: PLW0603
    # #393: max_length passed explicitly -- see MAX_LENGTH above.
    _model = CrossEncoder(MODEL_NAME, revision=MODEL_REVISION, max_length=MAX_LENGTH)
    metrics.model_loaded.set(1)
    yield
    metrics.model_loaded.set(0)


app = FastAPI(title="nexus-rag reranker-service", lifespan=lifespan)
app.add_middleware(_SecurityHeadersMiddleware)
# #134: request spans that honor the traceparent orchestration-mcp's httpx
# instrumentation sends, so this service's spans nest under the caller's
# `rerank` span.
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "model": MODEL_NAME, "loaded": _model is not None}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    payload, content_type = metrics.render()
    return Response(payload, media_type=content_type)


class Chunk(BaseModel):
    id: str
    text: str


class RerankRequest(BaseModel):
    query: str
    chunks: list[Chunk]


class RerankedChunk(BaseModel):
    id: str
    score: float


def _window_texts(  # type: ignore[no-untyped-def]
    tokenizer, query: str, text: str, max_length: int
) -> tuple[list[str], bool]:
    """#393: the texts whose max score stands in for this chunk's score, and
    whether the raw (query, text) pair was oversized.

    Returns ([text], False) unchanged when the pair fits max_length.
    Otherwise splits the chunk's tokens into overlapping windows that each
    fit alongside the query, decoded back to text -- so a relevant passage in
    the chunk's tail is scored instead of silently cut -- and returns those
    windows with True. When the query alone leaves no budget for windowing,
    windowing cannot help and the model's own truncation is the only
    remaining behavior, but the pair is still oversized: texts is [text]
    unchanged, oversized is still True, so the caller's metric counts this
    chunk rather than silently missing every over-length query. Never
    returns an empty texts list.
    """
    query_ids = tokenizer(query, add_special_tokens=False)["input_ids"]
    text_ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    # [CLS] query [SEP] text [SEP] -> 3 special tokens around the pair.
    budget = max_length - len(query_ids) - 3
    oversized = len(text_ids) > budget
    if budget <= 0 or not oversized:
        return [text], oversized
    stride = max(1, int(budget * (1 - _WINDOW_OVERLAP)))
    windows: list[str] = []
    for start in range(0, len(text_ids), stride):
        windows.append(tokenizer.decode(text_ids[start : start + budget]))
        if start + budget >= len(text_ids):
            break
    return windows, True


@app.post(
    "/rerank",
    response_model=list[RerankedChunk],
    dependencies=[Depends(_check_shared_secret)],
)
def rerank(body: RerankRequest) -> list[RerankedChunk]:
    if _model is None:
        metrics.requests_total.labels(outcome="unavailable").inc()
        raise RuntimeError("model not loaded")
    if not body.chunks:
        metrics.batch_chunks.observe(0)
        metrics.requests_total.labels(outcome="empty").inc()
        return []
    if len(body.chunks) > MAX_RERANK_CHUNKS:
        metrics.batch_chunks.observe(len(body.chunks))
        metrics.requests_total.labels(outcome="too_large").inc()
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"batch of {len(body.chunks)} chunks exceeds the {MAX_RERANK_CHUNKS}-chunk limit",
        )
    # #393: an oversized (query, chunk) pair used to be truncated silently by
    # the tokenizer -- the chunk was ranked on its head. Now every oversized
    # chunk is at least counted (nexus_rag_reranker_oversized_chunks_total),
    # and with WINDOW_SCORING on it is scored as the max over overlapping
    # windows so its tail can contribute.
    pairs: list[tuple[str, str]] = []
    owner: list[int] = []
    oversized = 0
    tokenizer = _model.tokenizer
    for i, chunk in enumerate(body.chunks):
        texts, is_oversized = _window_texts(tokenizer, body.query, chunk.text, MAX_LENGTH)
        if is_oversized:
            oversized += 1
            if not WINDOW_SCORING:
                texts = [chunk.text]
        pairs.extend((body.query, t) for t in texts)
        owner.extend([i] * len(texts))
    if oversized:
        metrics.oversized_chunks_total.labels(
            handling="windowed" if WINDOW_SCORING else "truncated"
        ).inc(oversized)
    # #134: the CPU-bound stage this service exists for, as its own span --
    # batch size only, never query/chunk text (common/tracing.py's rule).
    with tracer.start_as_current_span("model.predict", attributes={"rerank.pairs": len(pairs)}):
        try:
            with metrics.model_predict_seconds.time():
                scores = _model.predict(pairs)
        except Exception:
            metrics.requests_total.labels(outcome="error").inc()
            raise
    chunk_scores: list[float] = [float("-inf")] * len(body.chunks)
    for pair_index, score in enumerate(scores):
        i = owner[pair_index]
        chunk_scores[i] = max(chunk_scores[i], float(score))
    metrics.batch_chunks.observe(len(body.chunks))
    metrics.requests_total.labels(outcome="ok").inc()
    ranked = sorted(
        zip(body.chunks, chunk_scores, strict=True), key=lambda pair: pair[1], reverse=True
    )
    return [RerankedChunk(id=chunk.id, score=score) for chunk, score in ranked]
