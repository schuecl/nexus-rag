from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import KeycloakTokenVerifier
from app.server import app
from common import claims as claims_module


class _FakeJWKClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self._public_key)


@pytest.fixture(scope="session")
def rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_public_key(rsa_private_key):
    return rsa_private_key.public_key()


@pytest.fixture
def mint_token(rsa_private_key):
    def _mint(**overrides):
        payload = {
            "sub": "user-0001",
            "preferred_username": "bob-query",
            "iss": "http://keycloak:8080/realms/nexus-rag",
            "aud": "rag-app",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "scope": "openid profile email",
        }
        payload.update(overrides)
        return jwt.encode(payload, rsa_private_key, algorithm="RS256")

    return _mint


@pytest.fixture
def verified_env(monkeypatch, rsa_public_key):
    monkeypatch.setattr(
        claims_module,
        "OIDC_ISSUERS",
        ["http://keycloak:8080/realms/nexus-rag"],
    )
    monkeypatch.setattr(claims_module, "OIDC_AUDIENCE", "rag-app")
    monkeypatch.setattr(claims_module, "OIDC_SKIP_VERIFY", False)
    monkeypatch.setattr(claims_module, "_jwk_client", lambda: _FakeJWKClient(rsa_public_key))


async def test_valid_keycloak_token_is_adapted_for_fastmcp(verified_env, mint_token):
    token = mint_token(
        azp="rag-app",
        scope="openid profile email",
    )

    result = await KeycloakTokenVerifier().verify_token(token)

    assert result is not None
    assert result.client_id == "rag-app"
    assert result.subject == "user-0001"
    assert result.scopes == ["openid", "profile", "email"]
    assert result.claims == {"iss": "http://keycloak:8080/realms/nexus-rag"}


async def test_expired_keycloak_token_is_rejected(verified_env, mint_token):
    token = mint_token(exp=int(time.time()) - 1)

    assert await KeycloakTokenVerifier().verify_token(token) is None


async def test_mcp_transport_challenges_missing_bearer_token():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')


async def test_mcp_transport_challenges_expired_bearer_token(verified_env, mint_token):
    token = mint_token(exp=int(time.time()) - 1)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"
    assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')


async def test_health_route_remains_public():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
