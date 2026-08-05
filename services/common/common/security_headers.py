"""Shared ASGI middleware for the static response headers OWASP ZAP flagged
across all three HTTP-facing services (#444, #445): X-Content-Type-Options
and Referrer-Policy on every response, plus whatever extra static headers a
given service opts into (ingestion-api's X-Frame-Options, #444).

Plain ASGI middleware, not Starlette's BaseHTTPMiddleware or FastAPI's
`@app.middleware("http")` decorator: the latter two only exist on a FastAPI
app, but orchestration-mcp's app is the bare Starlette instance
MCPServer.streamable_http_app() returns. This class works unchanged on both
via `.add_middleware()`, which every ASGI app -- Starlette and its FastAPI
subclass alike -- provides.

reranker-service does not depend on this package by design (see its
app/main.py's inline _setup_tracing/_setup_profiling) and carries its own
small inline duplicate of this class instead of importing it.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# #445: MIME-sniffing removal for every response -- the concrete case is
# ingestion-api's document-content route serving back uploaded bytes, where
# sniffing is what would let a browser execute something served as inert.
# Referrer-Policy isn't the subject of either #444/#445 on its own, but both
# issues' own "suggested direction" flagged it as the other cheap static
# header worth shipping in the same middleware seam.
BASE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class SecurityHeadersMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        self.app = app
        self._headers = BASE_HEADERS + extra_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + list(self._headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)
