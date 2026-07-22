"""Core of the rag_search tool (FR-24..FR-29). Claims parsing and the mandatory
access filter (Section 6.1/FR-26) are real and enforced; hybrid dense+BM25
fusion and reranking (FR-24/FR-25) are TODO -- nothing has been embedded into
Qdrant yet (ingestion-api's FR-3..FR-6 are also deferred this session), so this
executes the real filter against Qdrant and reports what it found (or that the
collection doesn't exist yet) rather than fabricating results.
"""

from __future__ import annotations

import os

import httpx
import jwt
from common.claims import parse_claims
from common.classification import allowed_classifications
from common.db import get_session
from common.qdrant_filters import build_access_filter
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "nexus_rag_chunks")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

_qdrant = QdrantClient(url=QDRANT_URL)


async def _embed_query(query: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": query},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


async def run_rag_search(bearer_token: str, query: str, top_k: int = 5) -> dict:
    try:
        claims = parse_claims(bearer_token)
    except jwt.PyJWTError as exc:
        return {"error": f"invalid token: {exc}"}

    if not claims.can_query:
        return {"error": "missing rag-query role"}

    with next(get_session()) as session:
        allowed = allowed_classifications(session, claims.clearance)

    access_filter = build_access_filter(claims, allowed_classifications=allowed)

    result: dict = {
        "query": query,
        "user": claims.preferred_username,
        "applied_filter": access_filter.model_dump(exclude_none=True),
        "hybrid_retrieval": "TODO: dense+BM25 fusion not yet implemented (FR-24)",
        "reranking": "TODO: cross-encoder rerank not yet wired in (FR-25)",
    }

    try:
        query_vector = await _embed_query(query)
        hits = _qdrant.query_points(
            collection_name=QDRANT_COLLECTION,
            query=query_vector,
            query_filter=access_filter,
            limit=top_k,
        ).points
        result["results"] = [
            {"id": str(h.id), "score": h.score, "payload": h.payload} for h in hits
        ]
        if not hits:
            result["note"] = (
                "no chunks matched -- expected until FR-3..FR-6 (parse/chunk/embed/"
                "store) are implemented and documents are approved"
            )
    except (UnexpectedResponse, httpx.HTTPError) as exc:
        result["results"] = []
        result["note"] = (
            f"Qdrant collection '{QDRANT_COLLECTION}' not queryable yet ({exc}); "
            "this is expected until ingestion writes vectors (FR-6)"
        )

    return result
