"""Issues #444/#445: the static response headers OWASP ZAP flagged as absent
across ingestion-api, orchestration-mcp and reranker-service.

Driven directly at the ASGI level (scope/receive/send), not through
starlette.testclient.TestClient -- every other service's own middleware/
security test in this repo avoids TestClient already (see e.g.
services/reranker-service/tests/test_main.py's docstring), and this module
has no service directory of its own to inherit that convention's rationale
from a comment, so it's worth restating here: TestClient pulls in
starlette's own HTTP client shim (httpx or, as of the version this repo
pins, httpx2, which isn't installed everywhere this module's tests run),
which is one more moving part a shared-library unit test has no reason to
depend on when driving the ASGI callable directly is just as direct and has
no transitive-dependency surface at all.
"""

from __future__ import annotations

import asyncio

from common.security_headers import SecurityHeadersMiddleware


async def _ok_app(scope, receive, send):  # type: ignore[no-untyped-def]
    if scope["type"] != "http":
        return
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def _receive():  # type: ignore[no-untyped-def]
    return {"type": "http.request"}


def _call(middleware: SecurityHeadersMiddleware, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def _send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    asyncio.run(middleware(scope, _receive, _send))
    return sent


def _headers(sent: list[dict]) -> dict[bytes, bytes]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])


class TestBaseHeaders:
    def test_nosniff_and_referrer_policy_present(self):
        middleware = SecurityHeadersMiddleware(_ok_app)

        headers = _headers(_call(middleware, {"type": "http", "method": "GET", "path": "/health"}))

        assert headers[b"x-content-type-options"] == b"nosniff"
        assert headers[b"referrer-policy"] == b"no-referrer"

    def test_does_not_clobber_the_route_s_own_headers(self):
        middleware = SecurityHeadersMiddleware(_ok_app)

        headers = _headers(_call(middleware, {"type": "http", "method": "GET", "path": "/data"}))

        assert headers[b"content-type"] == b"application/json"

    def test_no_extra_headers_means_no_x_frame_options(self):
        """orchestration-mcp/reranker-service opt into the base set only --
        #444's X-Frame-Options is ingestion-api's own addition, not a
        default every caller of this middleware inherits."""
        middleware = SecurityHeadersMiddleware(_ok_app)

        headers = _headers(_call(middleware, {"type": "http", "method": "GET", "path": "/health"}))

        assert b"x-frame-options" not in headers


class TestExtraHeaders:
    def test_extra_header_is_added_alongside_the_base_set(self):
        middleware = SecurityHeadersMiddleware(
            _ok_app, extra_headers=((b"x-frame-options", b"DENY"),)
        )

        headers = _headers(_call(middleware, {"type": "http", "method": "GET", "path": "/health"}))

        assert headers[b"x-frame-options"] == b"DENY"
        assert headers[b"x-content-type-options"] == b"nosniff"
        assert headers[b"referrer-policy"] == b"no-referrer"


class TestNonHttpScopePassthrough:
    def test_lifespan_scope_is_not_touched(self):
        calls: list[str] = []

        async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
            calls.append(scope["type"])

        middleware = SecurityHeadersMiddleware(_inner_app)

        _call(middleware, {"type": "lifespan"})

        assert calls == ["lifespan"]
