"""Exposes rag_search two ways, both on the single ASGI app FastMCP builds
(mcp_server.streamable_http_app()) -- deliberately not wrapped in an outer
Starlette/FastAPI app via Mount(), which was tried first and doesn't work:
mounting FastMCP's app under an extra prefix double-nests its internal /mcp
route to /mcp/mcp, and more importantly an outer app's default lifespan does
not cascade into the mounted sub-app's, so the streamable-http session
manager's task group is never started and every MCP call 500s. FastMCP's own
`custom_route` decorator (used below for /health and /debug/rag_search) adds
plain HTTP routes to the *same* app and lifespan, sidestepping both problems.
Verified against the real `mcp` client SDK, not just read from source -- see
the commit message for what was checked and how.

1. As an MCP tool at /mcp -- what LibreChat calls per Section 7.7. The bearer
   token (raw-forwarded via addUserJwtToken, or OBO-exchanged per Section
   7.7's recommendation -- this service can't tell the difference, and
   doesn't need to, since both arrive as a normal Authorization header on the
   streamable-http request) is read from the forwarded request itself, not
   passed as a tool argument.
2. As a plain REST endpoint at /debug/rag_search for curl-based smoke testing
   without needing an MCP client.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.rag_search import DEFAULT_TOP_K, MAX_TOP_K, run_rag_search

# FastMCP's default DNS-rebinding protection only allows Host headers of
# 127.0.0.1/localhost/::1 (see mcp.server.fastmcp.server.FastMCP.__init__),
# because it assumes the server binds to a loopback address. This service is
# reached over the docker-compose network as "orchestration-mcp:8002" (that's
# the Host header LibreChat's requests carry), which the default allowlist
# rejects with a 421. Extend the allowlist to include that hostname instead
# of disabling DNS-rebinding protection outright.
mcp_server = FastMCP(
    "nexus-rag-orchestration",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "orchestration-mcp:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    ),
)


# This docstring is FastMCP's literal tool description (LLM-facing, sent as
# the "description" field of every tool schema over MCP) -- keep it short.
# Longer prose here measurably hurts small local models' tool-call
# reliability: reproduced live (issue #99 follow-up, 2026-07-26) against
# qwen2.5:7b-instruct via Ollama with the real MCP-generated schema --
# swapping this multi-paragraph docstring for a one-line description raised
# correctly-formed tool_calls from 2/8 to 5/8 in an isolated A/B test (see
# the exp_full vs exp_hybrid bullet in docs/dev-setup.md for the full
# numbers). The auth/security context that used to live here is unaffected
# by the shortening: it's implementation rationale for future maintainers,
# not something the calling model needs repeated on every call --
# Authorization is read from the request's Authorization header (forwarded
# or OBO-exchanged by LibreChat per Section 7.7), never a client-supplied
# argument, so the access filter (Section 6.1) can't be spoofed from tool-
# call arguments; and every real response already carries its own
# "security_notice" field (app/rag_search.py) marking retrieved content as
# untrusted data, not instructions -- restating that in the tool
# description would just be redundant token cost, not a lost safeguard.
@mcp_server.tool()
async def rag_search(
    query: str,
    ctx: Context,
    # Bounded rather than a plain `int`: top_k drives the retrieval fan-out
    # (rag_search.py's hybrid_limit) and, through it, how many candidates the
    # cross-encoder scores in one reranker-service call. Expressed as an
    # Annotated Field so the constraint lands in the MCP tool schema the
    # calling model sees, not just in a server-side check it can't anticipate.
    top_k: Annotated[int, Field(ge=1, le=MAX_TOP_K)] = DEFAULT_TOP_K,
    content_type_boosts: dict[str, float] | None = None,
) -> dict:
    """Search the approved, access-filtered document corpus.

    content_type_boosts: optional per-content-type score multiplier, e.g.
    {"table": 1.2} to prefer table content for this query. Omit for the
    default weighting.
    """
    request = ctx.request_context.request
    bearer_token = request.headers.get("authorization") if request is not None else None
    if not bearer_token:
        return {"error": "no Authorization header on the MCP request"}
    return await run_rag_search(bearer_token, query, top_k, content_type_boosts=content_type_boosts)


@mcp_server.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp_server.custom_route("/debug/rag_search", methods=["POST"])
async def debug_rag_search(request: Request) -> JSONResponse:
    authorization = request.headers.get("authorization")
    if not authorization:
        return JSONResponse({"detail": "missing Authorization header"}, status_code=401)
    query = request.query_params.get("query")
    if not query:
        return JSONResponse({"detail": "missing query parameter"}, status_code=400)
    # Unlike the MCP tool above, nothing validates a raw query-string value for
    # us here -- a bare int() raised ValueError out of the route (a 500) on any
    # non-numeric input, and accepted arbitrarily large values on numeric ones.
    raw_top_k = request.query_params.get("top_k", str(DEFAULT_TOP_K))
    try:
        top_k = int(raw_top_k)
    except ValueError:
        return JSONResponse(
            {"detail": f"top_k must be an integer, got {raw_top_k!r}"}, status_code=400
        )
    if not 1 <= top_k <= MAX_TOP_K:
        return JSONResponse(
            {"detail": f"top_k must be between 1 and {MAX_TOP_K}, got {top_k}"},
            status_code=400,
        )
    result = await run_rag_search(authorization, query, top_k)
    return JSONResponse(result)


app = mcp_server.streamable_http_app()
