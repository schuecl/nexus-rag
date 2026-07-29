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

import logging
import os
from typing import Annotated

import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from pydantic import AnyHttpUrl, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import metrics
from app.rag_search import DEFAULT_TOP_K, MAX_QUERY_CHARS, MAX_TOP_K, run_rag_search
from common.claims import OIDC_ISSUERS, parse_claims
from common.log_safety import log_safe
from common.logging_setup import setup_logging
from common.siem import enable_siem_export
from common.tracing import setup_tracing

# #73: level-configurable structured logging (LOG_LEVEL/LOG_FORMAT), and NFR-2
# SIEM export of the FR-31 audit events every rag_search call writes
# (query, query.denied, ...).
setup_logging("orchestration-mcp")
enable_siem_export("orchestration-mcp")
# #134: the rag_search span tree (rag_search.py). httpx instrumentation adds
# the Ollama embedding call and the reranker-service hop as child spans and
# carries the trace context to the reranker. Disabled unless
# OTEL_EXPORTER_OTLP_ENDPOINT is set.
setup_tracing("orchestration-mcp")
HTTPXClientInstrumentor().instrument()

# FastMCP's default DNS-rebinding protection only allows Host headers of
# 127.0.0.1/localhost/::1 (see mcp.server.fastmcp.server.FastMCP.__init__),
# because it assumes the server binds to a loopback address. This service is
# reached over the docker-compose network as "orchestration-mcp:8002" (that's
# the Host header LibreChat's requests carry), which the default allowlist
# rejects with a 421. Extend the allowlist to include that hostname instead
# of disabling DNS-rebinding protection outright.
logger = logging.getLogger("orchestration-mcp")


class KeycloakTokenVerifier:
    """Issue #200: adapts the shared `common.claims.parse_claims` verifier
    (issuer/audience/signature/expiry, same check every other service in this
    repo uses) to FastMCP's `TokenVerifier` protocol, so an invalid or expired
    bearer is rejected by FastMCP's own auth middleware *before* a request
    reaches the streamable-HTTP session -- as an RFC 6750 401 `invalid_token`
    with a `WWW-Authenticate` challenge, not an HTTP 200 tool-call payload
    with an error string buried in it. That distinction is what lets
    LibreChat (or any OAuth-aware MCP client) recognize expiry and redeem its
    refresh token instead of leaving a stale bearer on a long-lived
    connection.

    This does not replace the token check inside `rag_search`
    (app/rag_search.py's `parse_claims` call): that one derives the actual
    `UserClaims` used to build the access filter, per FR-26, and also has to
    keep working for `/debug/rag_search`, which never goes through FastMCP's
    auth middleware. The two checks doing the same verification twice on a
    real MCP call is a deliberate no-weakening trade, not an oversight.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = parse_claims(token)
        except jwt.PyJWTError as exc:
            # Same reasoning as rag_search.py's #214 note: the exception type
            # (e.g. ExpiredSignatureError) is useful operator signal; PyJWT's
            # message text names the expected issuer/audience/algorithm,
            # which fingerprints the deployment for anyone probing with a
            # junk token.
            logger.warning(
                "MCP transport rejected bearer: %s: %s", type(exc).__name__, log_safe(exc)
            )
            return None
        return AccessToken(
            token=token,
            client_id=claims.sub,
            scopes=claims.rag_roles,
            subject=claims.sub,
        )


mcp_server = FastMCP(
    "nexus-rag-orchestration",
    token_verifier=KeycloakTokenVerifier(),
    # required_scopes deliberately omitted: `rag-query` authorization stays
    # inside rag_search (run_rag_search's `claims.can_query` check), which
    # audits a denied query (FR-31). Enforcing a scope here would let
    # FastMCP's middleware reject a role-less-but-authenticated caller before
    # that audit write ever happens.
    auth=AuthSettings(
        # Descriptive only here (no auth_server_provider, no
        # resource_server_url below) -- FastMCP doesn't use it to verify
        # anything itself; the actual issuer allowlist enforced by
        # parse_claims is common.claims.OIDC_ISSUERS in full.
        issuer_url=AnyHttpUrl(OIDC_ISSUERS[0]),
        resource_server_url=None,
    ),
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
    # #208: bounded like top_k, and as an Annotated Field for the same reason
    # -- the constraint lands in the MCP tool schema the calling model sees,
    # so an oversized query is refused before it costs an embedding call, a
    # sparse encode, and a cross-encoder pass on the shared model.
    query: Annotated[str, Field(max_length=MAX_QUERY_CHARS)],
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


@mcp_server.custom_route("/metrics", methods=["GET"])
async def prometheus_metrics(_request: Request) -> Response:
    """Issue #72: scrape surface for retrieval latency, outcome counts, and the
    reranker fallback rate.

    Unauthenticated, like /health, because a scrape target generally is -- and
    because it carries no corpus content: stage names and outcome names only,
    never a user, query, or document id (see app/metrics.py on why label
    cardinality is kept content-free).

    It is not *nothing*, though: aggregate query volume and result-count
    distributions are operational signal. Reaching it should be restricted at
    the network layer rather than left open to the namespace -- see the
    NetworkPolicy work in #110, which currently allows only ingestion-api and
    the configured MCP clients to reach this service, so a Prometheus scraper
    needs adding there explicitly.
    """
    payload, content_type = metrics.render()
    return Response(payload, media_type=content_type)


# #214: off unless explicitly enabled. This endpoint is a curl-shaped
# convenience for local work; it was also compiled into every production image
# with no way to turn it off. Authorization is enforced on it, so leaving it on
# is not a hole -- but it is surface that nothing in a deployed environment
# needs, and the least surprising default for a route named /debug is absent.
DEBUG_ENDPOINT_ENABLED = os.environ.get("DEBUG_RAG_SEARCH_ENABLED", "true").lower() == "true"


@mcp_server.custom_route("/debug/rag_search", methods=["POST"])
async def debug_rag_search(request: Request) -> JSONResponse:
    if not DEBUG_ENDPOINT_ENABLED:
        return JSONResponse({"detail": "not found"}, status_code=404)
    authorization = request.headers.get("authorization")
    if not authorization:
        return JSONResponse({"detail": "missing Authorization header"}, status_code=401)

    # #214: the query comes from the JSON body, not the query string. #125
    # removed query text from the audit log because a question asked of a
    # classified corpus is itself sensitive -- and then this route put the same
    # text into every proxy access log, ingress log, and browser history entry
    # in the path. The careful thing was done in one place and undone here.
    #
    # ?query= is still read as a fallback so existing scripts and the docs'
    # curl examples keep working, but it is deprecated and warned about.
    body: dict = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"detail": "malformed JSON body"}, status_code=400)

    query = body.get("query") or request.query_params.get("query")
    if query and not body.get("query"):
        logger.warning(
            "/debug/rag_search received the query in the URL; it will appear in "
            "proxy and ingress logs. Send it in a JSON body instead."
        )
    if not query:
        return JSONResponse(
            {"detail": 'missing query (send {"query": ...} as JSON)'}, status_code=400
        )
    # #208: the MCP tool's schema bounds this; a raw query string has nothing
    # validating it, exactly as was true of top_k before #106.
    if len(query) > MAX_QUERY_CHARS:
        return JSONResponse(
            {"detail": f"query must be at most {MAX_QUERY_CHARS} characters, got {len(query)}"},
            status_code=400,
        )
    # Unlike the MCP tool above, nothing validates a raw query-string value for
    # us here -- a bare int() raised ValueError out of the route (a 500) on any
    # non-numeric input, and accepted arbitrarily large values on numeric ones.
    raw_top_k = body.get("top_k", request.query_params.get("top_k", DEFAULT_TOP_K))
    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError):
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
