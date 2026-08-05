"""Issues #444/#445: the static response headers OWASP ZAP flagged as absent
across ingestion-api, orchestration-mcp and reranker-service.

Exercised against a minimal Starlette app rather than any real service's
app -- this module has no lifespan (no NATS, no live model load), so
TestClient carries none of the cost the services' own test suites avoid it
for (see e.g. services/ingestion-api/tests/test_login_gate.py).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from common.security_headers import SecurityHeadersMiddleware


def _app(extra_headers: tuple[tuple[bytes, bytes], ...] = ()) -> Starlette:
    async def _ok(_request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    async def _json(_request):  # type: ignore[no-untyped-def]
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/health", _ok), Route("/data", _json)])
    app.add_middleware(SecurityHeadersMiddleware, extra_headers=extra_headers)
    return app


class TestBaseHeaders:
    def test_nosniff_and_referrer_policy_present(self):
        client = TestClient(_app())

        response = client.get("/health")

        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_applies_to_every_route_not_just_one(self):
        client = TestClient(_app())

        for path in ("/health", "/data"):
            response = client.get(path)
            assert response.headers["x-content-type-options"] == "nosniff"

    def test_does_not_clobber_the_route_s_own_headers(self):
        client = TestClient(_app())

        response = client.get("/data")

        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"status": "ok"}

    def test_no_extra_headers_means_no_x_frame_options(self):
        """orchestration-mcp/reranker-service opt into the base set only --
        #444's X-Frame-Options is ingestion-api's own addition, not a
        default every caller of this middleware inherits."""
        client = TestClient(_app())

        response = client.get("/health")

        assert "x-frame-options" not in response.headers


class TestExtraHeaders:
    def test_extra_header_is_added_alongside_the_base_set(self):
        client = TestClient(_app(extra_headers=((b"x-frame-options", b"DENY"),)))

        response = client.get("/health")

        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


class TestNonHttpScopePassthrough:
    async def test_lifespan_scope_is_not_touched(self):
        calls = []

        async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
            calls.append(scope["type"])

        middleware = SecurityHeadersMiddleware(_inner_app)

        async def _receive():  # type: ignore[no-untyped-def]
            return {"type": "lifespan.startup"}

        async def _send(_message):  # type: ignore[no-untyped-def]
            pass

        await middleware({"type": "lifespan"}, _receive, _send)

        assert calls == ["lifespan"]
