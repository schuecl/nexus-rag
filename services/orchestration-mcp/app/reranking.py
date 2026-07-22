"""FR-25: cross-encoder reranking pass over the top-N hybrid candidates,
calling the already-standalone reranker-service rather than loading the
model in-process. Degrades to the fused (pre-rerank) order on a reranker
outage rather than failing the whole query -- reranking improves ranking
quality, it isn't the thing that keeps unauthorized content out (that's the
access filter applied before any of this), so it's reasonable to keep serving
degraded-but-authorized results rather than a hard failure.
"""

from __future__ import annotations

import os

import httpx

RERANKER_URL = os.environ.get("RERANKER_URL", "http://reranker-service:8003")


async def rerank(query: str, candidates: list[dict], top_k: int) -> tuple[list[dict], str]:
    """candidates: list of dicts each with at least "id" and a "payload" dict
    containing "text". Returns (reranked candidates truncated to top_k, status note)."""
    if not candidates:
        return [], "no candidates to rerank"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RERANKER_URL}/rerank",
                json={
                    "query": query,
                    "chunks": [
                        {"id": c["id"], "text": c["payload"].get("text", "")}
                        for c in candidates
                    ],
                },
            )
            resp.raise_for_status()
            scores = {row["id"]: row["score"] for row in resp.json()}
    except httpx.HTTPError as exc:
        return candidates[:top_k], f"reranker-service unavailable ({exc}); using fused order"

    ranked = sorted(candidates, key=lambda c: scores.get(c["id"], float("-inf")), reverse=True)
    return ranked[:top_k], "cross-encoder rerank via reranker-service (FR-25)"
