"""OIDC/JWT claim parsing shared by ingestion-api and orchestration-mcp.

Both ingestion-time tagging constraints (FR-18) and query-time access filtering
(FR-26) must be derived from the same claims, evaluated server-side -- this module
is that single source of truth (see REQUIREMENTS.md Section 6.1).
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from pydantic import BaseModel, Field

RAG_CURATE_PREFIX = "rag-curate:"
RAG_RELEASABILITY_PREFIX = "rag-releasability:"
RAG_CLEARANCE_PREFIX = "rag-clearance:"

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

if OIDC_SKIP_VERIFY:  # pragma: no cover - a startup-time side effect
    # Issue #215: this used to take effect in complete silence. The other
    # dev-credential fallbacks in this codebase degrade convenience if
    # misapplied; this one disables the signature check that every
    # Classification/Releasability/Access-scope decision ultimately rests on,
    # and a stack running with it set looks entirely healthy.
    #
    # Deliberately CRITICAL rather than WARNING, and emitted at import rather
    # than per-token: INFO/WARNING is the level real operational noise lives
    # at, so it is the level this would be scrolled past at, and one line per
    # verification would be noise nobody reads.
    logging.getLogger("claims").critical(
        "OIDC_SKIP_VERIFY=true -- JWT signatures are NOT being verified. Every "
        "identity, clearance, and releasability claim is accepted unchecked. "
        "This is a local-development escape hatch and must never be set in a "
        "deployed environment."
    )


class UserClaims(BaseModel):
    sub: str
    preferred_username: str
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
    def clearance(self) -> str:
        # FR-18/FR-26/FR-14: the user's Classification level, derived from a
        # rag-clearance:<value> client role -- same rag_roles-prefix pattern as
        # curatable_orgs/releasability below, but a single ranked value rather
        # than a set, so exactly one such role is expected per user. A stray
        # second role just wins by rag_roles order; that's a Keycloak-admin
        # misconfiguration to fix, not something this layer needs to guard
        # against (it has no DB access to the ClassificationLevel rank table
        # here anyway -- see common/classification.py).
        for role in self.rag_roles:
            if role.startswith(RAG_CLEARANCE_PREFIX):
                return role[len(RAG_CLEARANCE_PREFIX) :]
        return ""

    @property
    def releasability(self) -> list[str]:
        # FR-18/FR-20: which Releasability values this user holds, derived from
        # rag-releasability:<value> client roles -- the same rag_roles-prefix
        # pattern curatable_orgs below uses for rag-curate:<org> -- rather than
        # a free-text user attribute. Granting/revoking a caveat is then a
        # discoverable, admin-console role assignment (mirroring how curator
        # authority already works), not an attribute value a typo could
        # silently get wrong with no validation against the admin-configurable
        # ReleasabilityValue list (common/models.py).
        return [
            role[len(RAG_RELEASABILITY_PREFIX) :]
            for role in self.rag_roles
            if role.startswith(RAG_RELEASABILITY_PREFIX)
        ]

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
        groups=payload.get("groups", []),
        org=payload.get("org"),
        rag_roles=payload.get("rag_roles", []),
    )
