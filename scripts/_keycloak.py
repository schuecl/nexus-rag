"""Shared Keycloak password-grant helper for the dev-only scripts in this
directory (seed_sample_data.py, evaluate_retrieval.py) -- both need a token
for one of the seeded realm users (infra/keycloak/realm-export), and this
keeps that single piece of OAuth2 plumbing in one place rather than
duplicated per script.
"""

from __future__ import annotations

import os
import time

import httpx

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
REALM = "nexus-rag"
CLIENT_ID = "rag-app"
CLIENT_SECRET = os.environ.get("RAG_APP_KEYCLOAK_CLIENT_SECRET", "dev-rag-app-secret")
SEED_PASSWORD = "devpass123"  # matches infra/keycloak/realm-export -- dev-only


def get_token(username: str) -> str:
    resp = httpx.post(
        f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": SEED_PASSWORD,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def wait_until_up(urls: list[str], timeout_seconds: int = 120) -> None:
    """Poll each URL with a plain GET until all return a non-error status,
    or raise once timeout_seconds has elapsed."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for url in urls:
                httpx.get(url, timeout=5).raise_for_status()
            return
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"not all of {urls} were ready after {timeout_seconds}s: {last_error}")
