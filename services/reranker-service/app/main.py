"""FR-25: cross-encoder reranking pass over top-N hybrid retrieval candidates.
The only ML-serving piece besides embeddings that's fully functional this
session -- small enough to finish now, and orchestration-mcp's hybrid search
(TODO) will call this once it exists."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

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


_setup_tracing()
tracer = trace.get_tracer("reranker-service")

MODEL_NAME = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L6-v2")

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

_model: CrossEncoder | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # The module-level singleton is deliberate: the model is loaded once at
    # startup and read by both /health and /rerank, neither of which takes a
    # Request, so app.state isn't reachable from them without changing both
    # signatures. One process, one model, assigned exactly here.
    global _model  # noqa: PLW0603
    _model = CrossEncoder(MODEL_NAME)
    yield


app = FastAPI(title="nexus-rag reranker-service", lifespan=lifespan)
# #134: request spans that honor the traceparent orchestration-mcp's httpx
# instrumentation sends, so this service's spans nest under the caller's
# `rerank` span.
FastAPIInstrumentor.instrument_app(app)


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "model": MODEL_NAME, "loaded": _model is not None}


class Chunk(BaseModel):
    id: str
    text: str


class RerankRequest(BaseModel):
    query: str
    chunks: list[Chunk]


class RerankedChunk(BaseModel):
    id: str
    score: float


@app.post("/rerank", response_model=list[RerankedChunk])
def rerank(body: RerankRequest) -> list[RerankedChunk]:
    if _model is None:
        raise RuntimeError("model not loaded")
    if not body.chunks:
        return []
    if len(body.chunks) > MAX_RERANK_CHUNKS:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"batch of {len(body.chunks)} chunks exceeds the {MAX_RERANK_CHUNKS}-chunk limit",
        )
    pairs = [(body.query, chunk.text) for chunk in body.chunks]
    # #134: the CPU-bound stage this service exists for, as its own span --
    # batch size only, never query/chunk text (common/tracing.py's rule).
    with tracer.start_as_current_span(
        "model.predict", attributes={"rerank.pairs": len(pairs)}
    ):
        scores = _model.predict(pairs)
    ranked = sorted(
        zip(body.chunks, scores, strict=True), key=lambda pair: pair[1], reverse=True
    )
    return [RerankedChunk(id=chunk.id, score=float(score)) for chunk, score in ranked]
