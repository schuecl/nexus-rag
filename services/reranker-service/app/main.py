"""FR-25: cross-encoder reranking pass over top-N hybrid retrieval candidates.
The only ML-serving piece besides embeddings that's fully functional this
session -- small enough to finish now, and orchestration-mcp's hybrid search
(TODO) will call this once it exists."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

MODEL_NAME = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L6-v2")

_model: CrossEncoder | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _model
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
    pairs = [(body.query, chunk.text) for chunk in body.chunks]
    scores = _model.predict(pairs)
    ranked = sorted(
        zip(body.chunks, scores), key=lambda pair: pair[1], reverse=True
    )
    return [RerankedChunk(id=chunk.id, score=float(score)) for chunk, score in ranked]
