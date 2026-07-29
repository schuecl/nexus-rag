"""Coverage for issue #200: an invalid, expired, or missing bearer must be
rejected at the MCP transport boundary (RFC 6750 401 `invalid_token` plus a
`WWW-Authenticate` challenge), not returned as an HTTP 200 tool-call payload
with an error string buried inside it. That distinction is what lets an
OAuth-aware client like LibreChat recognize expiry and redeem its refresh
token instead of leaving a stale bearer on a long-lived connection -- see
app/server.py's KeycloakTokenVerifier docstring for the full rationale.
"""

from __future__ import annotations

import jwt
import pytest
from starlette.testclient import TestClient

from app import server

_MCP_ACCEPT_HEADER = {"accept": "application/json, text/event-stream"}


class _Claims:
    sub = "u1"
    preferred_username = "bob-query"
    rag_roles = ["rag-query"]  # noqa: RUF012 -- test double, not a dataclass


class TestKeycloakTokenVerifier:
    """Unit-level coverage of the FastMCP-facing adapter around the shared
    common.claims.parse_claims verifier."""

    async def test_valid_token_is_adapted_to_an_access_token(self, monkeypatch):
        monkeypatch.setattr(server, "parse_claims", lambda _t: _Claims())

        result = await server.KeycloakTokenVerifier().verify_token("sometoken")

        assert result is not None
        assert result.subject == "u1"
        assert result.client_id == "u1"
        assert result.scopes == ["rag-query"]

    @pytest.mark.parametrize(
        "exc",
        [jwt.ExpiredSignatureError("Signature has expired"), jwt.InvalidTokenError("bad token")],
    )
    async def test_invalid_or_expired_token_is_rejected(self, monkeypatch, exc):
        def _raise(_token):
            raise exc

        monkeypatch.setattr(server, "parse_claims", _raise)

        assert await server.KeycloakTokenVerifier().verify_token("stale") is None


class TestTransportRejectsBadBearers:
    """The acceptance criteria in issue #200: unauthenticated and expired
    bearers get a transport-level 401 with a bearer challenge before an MCP
    session is ever established, and /health stays reachable without one."""

    @pytest.fixture
    def client(self):
        return TestClient(server.app)

    def test_missing_bearer_is_rejected_with_a_bearer_challenge(self, client):
        resp = client.post("/mcp", headers=_MCP_ACCEPT_HEADER)

        assert resp.status_code == 401
        assert "invalid_token" in resp.headers["www-authenticate"]

    def test_expired_bearer_is_rejected_the_same_way_as_missing(self, client, monkeypatch):
        def _raise(_token):
            raise jwt.ExpiredSignatureError("Signature has expired")

        monkeypatch.setattr(server, "parse_claims", _raise)

        resp = client.post(
            "/mcp",
            headers={**_MCP_ACCEPT_HEADER, "authorization": "Bearer expired"},
        )

        assert resp.status_code == 401
        assert "invalid_token" in resp.headers["www-authenticate"]

    def test_health_stays_public(self, client):
        resp = client.get("/health")

        assert resp.status_code == 200
