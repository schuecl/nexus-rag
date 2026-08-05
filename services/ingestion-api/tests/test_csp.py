"""Issue #443: Content-Security-Policy on the document portal.

Behavioral coverage of ContentSecurityPolicyMiddleware itself is exercised
against a minimal Starlette app rather than the real app.main -- no lifespan
to avoid (same reasoning as test_security_headers.py). Wiring and the
template-side nonce contract are covered separately below.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app import main
from app.csp import ContentSecurityPolicyMiddleware


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()  # #188: undisposed sqlite3 connections fail an unrelated test at GC time


def _app() -> Starlette:
    async def _ok(_request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/health", _ok)])
    app.add_middleware(ContentSecurityPolicyMiddleware)
    return app


class TestPolicyHeader:
    def test_sends_a_csp_header(self):
        client = TestClient(_app())

        response = client.get("/health")

        assert "content-security-policy" in response.headers

    def test_policy_carries_the_required_directives(self):
        client = TestClient(_app())

        policy = client.get("/health").headers["content-security-policy"]

        assert "default-src 'self'" in policy
        assert "object-src 'none'" in policy
        assert "base-uri 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "script-src 'self' 'nonce-" in policy

    def test_nonce_changes_between_requests(self):
        client = TestClient(_app())

        first = client.get("/health").headers["content-security-policy"]
        second = client.get("/health").headers["content-security-policy"]

        assert first != second

    async def test_scope_state_nonce_matches_the_header_nonce(self):
        """The value _page_context reads off request.state must be the exact
        same value the response header carries -- a template rendering a
        stale/different nonce would be silently blocked."""
        seen: dict[str, str] = {}

        async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
            seen["nonce"] = scope["state"]["csp_nonce"]
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = ContentSecurityPolicyMiddleware(_inner_app)
        sent: list[dict] = []

        async def _receive():  # type: ignore[no-untyped-def]
            return {"type": "http.request"}

        async def _send(message):  # type: ignore[no-untyped-def]
            sent.append(message)

        await middleware({"type": "http", "method": "GET", "path": "/x"}, _receive, _send)

        start = next(m for m in sent if m["type"] == "http.response.start")
        header_policy = next(v for k, v in start["headers"] if k == b"content-security-policy")
        assert f"'nonce-{seen['nonce']}'".encode() in header_policy


class TestNonHttpScopePassthrough:
    async def test_lifespan_scope_is_not_touched(self):
        calls = []

        async def _inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
            calls.append(scope["type"])

        middleware = ContentSecurityPolicyMiddleware(_inner_app)

        async def _receive():  # type: ignore[no-untyped-def]
            return {"type": "lifespan.startup"}

        async def _send(_message):  # type: ignore[no-untyped-def]
            pass

        await middleware({"type": "lifespan"}, _receive, _send)

        assert calls == ["lifespan"]


class TestWiring:
    def test_middleware_is_installed_on_the_real_app(self):
        installed = [
            m for m in main.app.user_middleware if m.cls is ContentSecurityPolicyMiddleware
        ]

        assert len(installed) == 1


class TestPageContext:
    def test_page_context_reads_the_nonce_off_request_state(self, db):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "query_string": b"",
                "headers": [],
                "app": main.app,
                "state": {"csp_nonce": "abc123"},
            }
        )

        ctx = main._page_context(request, db, None)

        assert ctx["csp_nonce"] == "abc123"
