"""FR-25: cross-encoder reranking pass over top-N hybrid retrieval candidates.
The only ML-serving piece besides embeddings that's fully functional this
session -- small enough to finish now, and orchestration-mcp's hybrid search
(TODO) will call this once it exists."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
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
async def lifespan(_app: FastAPI):
    # The module-level singleton is deliberate: the model is loaded once at
    # startup and read by both /health and /rerank, neither of which takes a
    # Request, so app.state isn't reachable from them without changing both
    # signatures. One process, one model, assigned exactly here.
    global _model  # noqa: PLW0603
    _model = CrossEncoder(MODEL_NAME)
    yield


app = FastAPI(title="nexus-rag reranker-service", lifespan=lifespan)


@app.get("/health")
def health():
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
def rerank(body: RerankRequest):
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
    scores = _model.predict(pairs)
    ranked = sorted(
        zip(body.chunks, scores, strict=True), key=lambda pair: pair[1], reverse=True
    )
    return [RerankedChunk(id=chunk.id, score=float(score)) for chunk, score in ranked]
