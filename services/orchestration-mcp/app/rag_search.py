"""Core of the rag_search tool (FR-24..FR-29). Claims parsing and the mandatory
access filter (Section 6.1/FR-26) are real and enforced, and ingestion-api now
writes real chunk vectors (FR-3..FR-6), so a query against an approved
document should return real hits. Hybrid dense+BM25 fusion and reranking
(FR-24/FR-25) are still TODO -- this executes a dense-only query against
Qdrant and reports what it found (or that the collection doesn't exist yet,
e.g. before any document has been submitted) rather than fabricating results.
"""

from __future__ import annotations

import os

import httpx
import jwt
from common.claims import parse_claims
from common.classification import allowed_classifications
from common.db import get_session
from common.qdrant_filters import build_access_filter
from common.qdrant_store import QDRANT_COLLECTION, get_qdrant_client
from qdrant_client.http.exceptions import UnexpectedResponse

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")


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
        hits = get_qdrant_client().query_points(
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
                "no chunks matched -- either nothing's been ingested/approved yet, "
                "or nothing in the corpus passes this user's access filter"
            )
    except (UnexpectedResponse, httpx.HTTPError) as exc:
        result["results"] = []
        result["note"] = (
            f"Qdrant collection '{QDRANT_COLLECTION}' not queryable ({exc}); it's "
            "created lazily on first ingestion (common.qdrant_store.ensure_collection), "
            "so this is expected if no document has been submitted yet"
        )

    return result
