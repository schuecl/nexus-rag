"""Exposes rag_search two ways:

1. As an MCP tool (mounted at /mcp) -- what LibreChat calls per Section 7.7.
   NOTE: the official OBO/JWT-forwarding wiring (Section 6.1/7.7) is deferred
   this session, so the tool takes the bearer token as an explicit argument
   rather than pulling it from a forwarded Authorization header -- swap this
   for automatic header extraction once OBO end-to-end is confirmed working
   (see REQUIREMENTS.md Section 8 open question on Keycloak 26.2+).
2. As a plain REST endpoint (mounted at /) for curl-based smoke testing
   without needing an MCP client, since nothing else in this skeleton can
   drive an MCP tool call yet.
"""

from __future__ import annotations

from app.rag_search import run_rag_search
from fastapi import FastAPI, Header, HTTPException
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

mcp_server = FastMCP("nexus-rag-orchestration")


@mcp_server.tool()
async def rag_search(query: str, authorization_token: str, top_k: int = 5) -> dict:
    """Search the approved, access-filtered document corpus (FR-24..FR-29).

    authorization_token: the caller's Keycloak bearer token (raw or OBO-exchanged),
    used to derive the mandatory Classification/Releasability/Access-scope filter.
    """
    return await run_rag_search(authorization_token, query, top_k)


debug_api = FastAPI(title="orchestration-mcp debug API")


@debug_api.get("/health")
def health():
    return {"status": "ok"}


@debug_api.post("/debug/rag_search")
async def debug_rag_search(
    query: str, top_k: int = 5, authorization: str | None = Header(default=None)
):
    if not authorization:
        raise HTTPException(401, "missing Authorization header")
    return await run_rag_search(authorization, query, top_k)


app = Starlette(
    routes=[
        Mount("/mcp", app=mcp_server.streamable_http_app()),
        Mount("/", app=debug_api),
    ]
)
