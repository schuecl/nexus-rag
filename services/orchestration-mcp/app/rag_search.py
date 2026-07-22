"""Core of the rag_search tool (FR-24..FR-29). Claims parsing and the mandatory
access filter (Section 6.1/FR-26) are real and enforced, ingestion-api writes
real chunk vectors (FR-3..FR-6), and retrieval is now genuinely hybrid: a
dense (semantic) leg and a BM25 sparse (keyword) leg are queried in parallel
via Qdrant's native Prefetch/FusionQuery API and combined with Reciprocal
Rank Fusion (FR-24), then the fused top-N candidates are reranked by the
standalone reranker-service before the final top-K is returned (FR-25).

The access_filter is applied to *both* prefetch legs, not just one -- FR-26
has to hold regardless of which retrieval path a chunk was found through, so
neither leg can be used to bypass it.

FR-31: every query attempt is written to the audit log -- including a denied
attempt (missing rag-query role) and a Qdrant-unreachable failure, not just
successful ones -- keyed on the caller's OIDC identity, same as ingestion
and curation events already are (app/routes/upload.py, app/routes/curate.py).
"""

from __future__ import annotations

import os

import httpx
import jwt
from app.reranking import rerank
from common.claims import UserClaims, parse_claims
from common.classification import allowed_classifications
from common.db import get_session
from common.models import AuditLogEntry
from common.qdrant_filters import build_access_filter
from common.qdrant_store import DENSE_VECTOR, QDRANT_COLLECTION, SPARSE_VECTOR, get_qdrant_client
from common.sparse_embedding import embed_sparse
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Fusion, FusionQuery, Prefetch

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

# How many fused candidates to hand to the reranker before truncating to the
# caller's requested top_k -- reranking over a wider pool than the final
# answer size is the point of FR-25 (a bigger lever than picking top_k straight
# out of retrieval).
HYBRID_CANDIDATE_MULTIPLIER = 4
MIN_HYBRID_CANDIDATES = 20


async def _embed_query(query: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": query},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


def _audit(claims: UserClaims, action: str, detail: dict) -> None:
    with next(get_session()) as session:
        session.add(
            AuditLogEntry(
                actor_sub=claims.sub,
                actor_username=claims.preferred_username,
                action=action,
                detail=detail,
            )
        )
        session.commit()


async def run_rag_search(bearer_token: str, query: str, top_k: int = 5) -> dict:
    try:
        claims = parse_claims(bearer_token)
    except jwt.PyJWTError as exc:
        # No reliably-identified actor to key an audit entry on (the token
        # itself didn't validate) -- nothing meaningful to log here.
        return {"error": f"invalid token: {exc}"}

    if not claims.can_query:
        _audit(claims, "query.denied", {"query": query, "reason": "missing rag-query role"})
        return {"error": "missing rag-query role"}

    with next(get_session()) as session:
        allowed = allowed_classifications(session, claims.clearance)

    access_filter = build_access_filter(claims, allowed_classifications=allowed)
    filter_summary = access_filter.model_dump(exclude_none=True)

    result: dict = {
        "query": query,
        "user": claims.preferred_username,
        "applied_filter": filter_summary,
    }

    hybrid_limit = max(top_k * HYBRID_CANDIDATE_MULTIPLIER, MIN_HYBRID_CANDIDATES)

    try:
        dense_vector = await _embed_query(query)
        sparse_vector = embed_sparse([query])[0]
        hits = get_qdrant_client().query_points(
            collection_name=QDRANT_COLLECTION,
            prefetch=[
                Prefetch(
                    query=dense_vector,
                    using=DENSE_VECTOR,
                    filter=access_filter,
                    limit=hybrid_limit,
                ),
                Prefetch(
                    query=sparse_vector,
                    using=SPARSE_VECTOR,
                    filter=access_filter,
                    limit=hybrid_limit,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=hybrid_limit,
        ).points
    except (UnexpectedResponse, httpx.HTTPError) as exc:
        result["hybrid_retrieval"] = "dense+bm25 RRF fusion (FR-24)"
        result["reranking"] = "skipped, no candidates"
        result["results"] = []
        result["note"] = (
            f"Qdrant collection '{QDRANT_COLLECTION}' not queryable ({exc}); it's "
            "created lazily on first ingestion (common.qdrant_store.ensure_collection), "
            "so this is expected if no document has been submitted yet"
        )
        _audit(
            claims,
            "query",
            {
                "query": query,
                "top_k": top_k,
                "applied_filter": filter_summary,
                "result_count": 0,
                "note": result["note"],
            },
        )
        return result

    result["hybrid_retrieval"] = f"dense+bm25 RRF fusion over {len(hits)} candidates (FR-24)"

    if not hits:
        result["reranking"] = "skipped, no candidates"
        result["results"] = []
        result["note"] = (
            "no chunks matched -- either nothing's been ingested/approved yet, "
            "or nothing in the corpus passes this user's access filter"
        )
        _audit(
            claims,
            "query",
            {
                "query": query,
                "top_k": top_k,
                "applied_filter": filter_summary,
                "result_count": 0,
                "note": result["note"],
            },
        )
        return result

    candidates = [{"id": str(h.id), "score": h.score, "payload": h.payload} for h in hits]
    reranked, rerank_note = await rerank(query, candidates, top_k)
    result["reranking"] = rerank_note
    result["results"] = reranked

    _audit(
        claims,
        "query",
        {
            "query": query,
            "top_k": top_k,
            "applied_filter": filter_summary,
            "result_count": len(reranked),
            "result_document_ids": [r["payload"].get("document_id") for r in reranked],
        },
    )

    return result
