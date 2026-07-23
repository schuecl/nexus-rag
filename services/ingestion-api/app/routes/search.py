"""Query-testing UI for a logged-in user (FR-24..FR-29's rag_search, browser
side) -- proxies to orchestration-mcp's existing /debug/rag_search REST
endpoint with the caller's own access token forwarded unchanged, so the
access filter applied is exactly what that same user would get through
LibreChat's real MCP path (ARCHITECTURE.md Section 4.3). This is a
testing/debugging aid, not a LibreChat replacement -- the production query
path stays LibreChat -> OBO token exchange -> MCP tool call.

Claims/role enforcement (rag-query) and the actual filter/retrieval/rerank
logic all stay in orchestration-mcp (app/rag_search.py) -- this route does no
enforcement of its own, just forwards the token and renders whatever comes
back, including an `{"error": ...}` body for a denied or invalid token
(run_rag_search returns that with a 200, not a 4xx, so there's nothing to
translate here).
"""

from __future__ import annotations

import os

import httpx
from app.deps import get_current_access_token
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/search", tags=["search"])

ORCHESTRATION_MCP_URL = os.environ.get("ORCHESTRATION_MCP_URL", "http://orchestration-mcp:8002")


@router.get("/query")
def search_query(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=20),
    token: str = Depends(get_current_access_token),
) -> dict:
    resp = httpx.post(
        f"{ORCHESTRATION_MCP_URL}/debug/rag_search",
        params={"query": query, "top_k": top_k},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
