"""OAuth resource-server authentication for the MCP transport.

The retrieval function still derives its access filter from ``parse_claims``.
This verifier moves the same JWT validation one layer earlier as well, so an
expired bearer token produces the HTTP 401 challenge an OAuth-capable MCP
client needs in order to refresh and retry. Returning an ``{"error": ...}``
tool result over HTTP 200 cannot trigger that protocol-level recovery.
"""

from __future__ import annotations

from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken

from common.claims import OIDC_AUDIENCE, decode_verified_token


def _client_id(payload: dict[str, Any]) -> str:
    """Choose the stable OAuth client identifier carried by a Keycloak JWT."""
    authorized_party = payload.get("azp")
    if isinstance(authorized_party, str) and authorized_party:
        return authorized_party

    audience = payload.get("aud")
    if isinstance(audience, str) and audience:
        return audience
    if isinstance(audience, list):
        first = next((value for value in audience if isinstance(value, str) and value), None)
        if first is not None:
            return first

    # Audience verification already required OIDC_AUDIENCE to be present.
    return OIDC_AUDIENCE


class KeycloakTokenVerifier:
    """Adapt the shared Keycloak JWT validator to FastMCP's auth protocol."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            payload = decode_verified_token(token)
        except (jwt.PyJWTError, KeyError, TypeError, ValueError):
            # FastMCP's bearer middleware turns ``None`` into the RFC 6750
            # 401/invalid_token response that LibreChat recognizes.
            return None

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            return None

        raw_scope = payload.get("scope", "")
        scopes = raw_scope.split() if isinstance(raw_scope, str) else []
        raw_expiry = payload.get("exp")
        expires_at = int(raw_expiry) if isinstance(raw_expiry, (int, float)) else None

        return AccessToken(
            token=token,
            client_id=_client_id(payload),
            scopes=scopes,
            expires_at=expires_at,
            subject=subject,
            # FastMCP uses issuer + subject + client_id to keep an authenticated
            # streamable-HTTP session bound to the same principal across an
            # access-token refresh.
            claims={"iss": payload.get("iss")},
        )
