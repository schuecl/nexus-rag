"""Issues #444/#445 (nosniff/Referrer-Policy only -- #444's X-Frame-Options is
ingestion-api's own concern, this service serves no HTML). This service
doesn't depend on services/common (see app/main.py's inline _setup_tracing/
_setup_profiling for the same split), so it carries its own inline
_SecurityHeadersMiddleware rather than importing
common.security_headers.SecurityHeadersMiddleware -- covered directly here
rather than through TestClient, same reasoning as test_main.py's shared-secret
tests (going through the app would trigger the lifespan's real CrossEncoder
load).

Plain `asyncio.run()` rather than async def tests: this service carries no
pytest-asyncio dependency (every route in app/main.py is sync, /rerank
included), so an `async def test_...` would silently never run under plain
pytest instead of failing loudly.
"""

from __future__ import annotations

import asyncio

from app import main


def test_security_headers_middleware_is_installed():
    installed = [m for m in main.app.user_middleware if m.cls is main._SecurityHeadersMiddleware]

    assert len(installed) == 1


def test_adds_nosniff_and_referrer_policy():
    sent: list[dict] = []

    async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def _receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request"}

    async def _send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    async def _run():  # type: ignore[no-untyped-def]
        middleware = main._SecurityHeadersMiddleware(_inner_app)
        await middleware({"type": "http", "method": "GET", "path": "/health"}, _receive, _send)

    asyncio.run(_run())

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert (b"x-content-type-options", b"nosniff") in start["headers"]
    assert (b"referrer-policy", b"no-referrer") in start["headers"]


def test_non_http_scope_passes_through_untouched():
    calls: list[str] = []

    async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
        calls.append(scope["type"])

    async def _receive():  # type: ignore[no-untyped-def]
        return {"type": "lifespan.startup"}

    async def _send(_message):  # type: ignore[no-untyped-def]
        pass

    async def _run():  # type: ignore[no-untyped-def]
        middleware = main._SecurityHeadersMiddleware(_inner_app)
        await middleware({"type": "lifespan"}, _receive, _send)

    asyncio.run(_run())

    assert calls == ["lifespan"]
