"""Issue #443: Content-Security-Policy on the document portal.

Behavioral coverage of ContentSecurityPolicyMiddleware itself is driven
directly at the ASGI level (scope/receive/send) rather than through
starlette.testclient.TestClient: TestClient pulls in starlette's own HTTP
client shim (httpx2, as of the version this repo pins), which isn't a real
dependency of anything this test needs to prove and isn't installed
everywhere this suite runs (see tests/unit/common/test_security_headers.py's
docstring for the same reasoning, applied there first). Wiring and the
template-side nonce contract are covered separately below.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app import main
from app.csp import ContentSecurityPolicyMiddleware


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()  # #188: undisposed sqlite3 connections fail an unrelated test at GC time


async def _ok_app(scope, receive, send):  # type: ignore[no-untyped-def]
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _receive():  # type: ignore[no-untyped-def]
    return {"type": "http.request"}


async def _call(middleware: ContentSecurityPolicyMiddleware, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def _send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    await middleware(scope, _receive, _send)
    return sent


def _policy(sent: list[dict]) -> bytes:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return next(v for k, v in start["headers"] if k == b"content-security-policy")


class TestPolicyHeader:
    async def test_sends_a_csp_header(self):
        middleware = ContentSecurityPolicyMiddleware(_ok_app)

        sent = await _call(middleware, {"type": "http", "method": "GET", "path": "/health"})

        start = next(m for m in sent if m["type"] == "http.response.start")
        assert any(k == b"content-security-policy" for k, _ in start["headers"])

    async def test_policy_carries_the_required_directives(self):
        middleware = ContentSecurityPolicyMiddleware(_ok_app)

        policy = _policy(
            await _call(middleware, {"type": "http", "method": "GET", "path": "/health"})
        )

        assert b"default-src 'self'" in policy
        assert b"object-src 'none'" in policy
        assert b"base-uri 'self'" in policy
        assert b"frame-ancestors 'none'" in policy
        assert b"script-src 'self' 'nonce-" in policy

    async def test_nonce_changes_between_requests(self):
        middleware = ContentSecurityPolicyMiddleware(_ok_app)

        first = _policy(
            await _call(middleware, {"type": "http", "method": "GET", "path": "/health"})
        )
        second = _policy(
            await _call(middleware, {"type": "http", "method": "GET", "path": "/health"})
        )

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
