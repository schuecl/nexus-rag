"""Issue #443: Content-Security-Policy on the document portal's HTML
responses, the defense-in-depth layer behind Jinja2 autoescaping (today's
only defense against a missed-escape XSS reaching a curator/admin session --
see test_template_xss.py).

Per-response nonce rather than externalizing every inline `<script>` block
(the other option #443 considered): curate.html and curate_list.html
interpolate live Jinja2 data (CLASSIFICATIONS, RELEASABILITY, CAN_PURGE)
directly into the script body, which a static .js file can't carry without a
separate JSON-island rearchitecture -- the nonce keeps that in place with a
much smaller diff, and is the direction the issue itself argued for.

ingestion-api-specific rather than services/common: orchestration-mcp and
reranker-service serve no HTML, so a CSP is meaningless on either.
"""

from __future__ import annotations

import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# script-src carries only 'self' (the static/*.js files) and the per-request
# nonce (the inline blocks) -- no 'unsafe-inline'. style-src keeps
# 'unsafe-inline' for the inline `style="background:..."` swatches in
# admin.html's theme picker; nonce-based style-src isn't worth the extra
# plumbing for a handful of static color values with no user input in them.
# img-src stays open to https:/data: because the admin-configurable branding
# logo (#248, also reused as the favicon) is an arbitrary operator-set URL,
# not a fixed asset this policy can pin to 'self'.
_POLICY_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class ContentSecurityPolicyMiddleware:
    """Generates one nonce per request, exposes it at
    `request.state.csp_nonce` (via `scope["state"]`, which Starlette's
    `Request.state` reads from directly) for route handlers to pass into the
    Jinja2 context, and sends the resulting policy on every response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        policy = _POLICY_TEMPLATE.format(nonce=nonce).encode()

        async def send_with_csp(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"content-security-policy", policy),
                ]
            await send(message)

        await self.app(scope, receive, send_with_csp)
