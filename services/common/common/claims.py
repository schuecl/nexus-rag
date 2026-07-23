"""OIDC/JWT claim parsing shared by ingestion-api and orchestration-mcp.

Both ingestion-time tagging constraints (FR-18) and query-time access filtering
(FR-26) must be derived from the same claims, evaluated server-side -- this module
is that single source of truth (see REQUIREMENTS.md Section 6.1).
"""

from __future__ import annotations

import os
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from pydantic import BaseModel, Field

RAG_CURATE_PREFIX = "rag-curate:"

# Comma-separated list of `iss` claim values to accept. Not a hypothetical: in the
# dev Compose stack, the same Keycloak instance is reachable -- and issues tokens
# -- under two different hostnames depending on who's asking, and Keycloak's
# default (no fixed KC_HOSTNAME) behavior stamps `iss` with whichever hostname the
# token request actually used: `http://keycloak:8080` for other containers on the
# Docker network (scripts/_keycloak.py, and the ingestion UI's own server-side
# OIDC login token exchange -- app/routes/auth.py), and `http://localhost:8080`
# for a human's curl from outside it (docs/dev-setup.md's "Getting a token"
# instructions, for direct API testing). Both are legitimate tokens from the
# same realm and have to be accepted; production (a single real external Keycloak,
# one canonical hostname -- see helm/nexus-rag/values.yaml's externalKeycloak) never
# needs more than one entry here. The first entry is also what JWKS gets fetched
# from below, so it has to be a URL this container can actually reach over the
# network -- `localhost` from inside a container would not resolve to Keycloak.
OIDC_ISSUERS = [
    v.strip()
    for v in os.environ.get(
        "OIDC_ISSUERS",
        "http://keycloak:8080/realms/nexus-rag,http://localhost:8080/realms/nexus-rag",
    ).split(",")
    if v.strip()
]
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", "rag-app")
# Dev-only escape hatch: skip signature verification when running against a
# throwaway local Keycloak without a reachable JWKS endpoint yet. Never set in prod.
OIDC_SKIP_VERIFY = os.environ.get("OIDC_SKIP_VERIFY", "false").lower() == "true"


class UserClaims(BaseModel):
    sub: str
    preferred_username: str
    clearance: str
    releasability: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    org: str | None = None
    rag_roles: list[str] = Field(default_factory=list)

    @property
    def can_ingest(self) -> bool:
        return "rag-ingest" in self.rag_roles

    @property
    def can_query(self) -> bool:
        return "rag-query" in self.rag_roles

    @property
    def curatable_orgs(self) -> list[str]:
        return [
            role[len(RAG_CURATE_PREFIX) :]
            for role in self.rag_roles
            if role.startswith(RAG_CURATE_PREFIX)
        ]

    def can_curate_org(self, org: str) -> bool:
        return org in self.curatable_orgs


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    return PyJWKClient(f"{OIDC_ISSUERS[0]}/protocol/openid-connect/certs")


def parse_claims(bearer_token: str) -> UserClaims:
    """Verify a Keycloak-issued access token and extract the claims defined in
    REQUIREMENTS.md Section 6.2. Raises jwt.PyJWTError on an invalid/expired token.
    """
    token = bearer_token.removeprefix("Bearer ").strip()

    if OIDC_SKIP_VERIFY:
        payload = jwt.decode(token, options={"verify_signature": False})
    else:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=OIDC_AUDIENCE,
            issuer=OIDC_ISSUERS,
        )

    return UserClaims(
        sub=payload["sub"],
        preferred_username=payload.get("preferred_username", payload["sub"]),
        clearance=payload.get("clearance", ""),
        releasability=payload.get("releasability", []),
        groups=payload.get("groups", []),
        org=payload.get("org"),
        rag_roles=payload.get("rag_roles", []),
    )
