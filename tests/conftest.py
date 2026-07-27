"""Shared fixtures: an in-memory RS256 keypair and a factory that mints
Keycloak-shaped access tokens (claim schema per REQUIREMENTS.md Section 6.2,
personas per docs/dev-setup.md's seeded users), so claims-parsing tests verify
real signatures without a live Keycloak.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

# Make the shared `common` package importable without an install step.
# Skipped under mutmut (e2e.yml's mutation job, which sets MUTANT_UNDER_TEST):
# there, mutmut's own sys.path setup must resolve `common` to the *mutated*
# copy -- inserting the original path here would shadow every mutant and make
# them all spuriously "survive".
_COMMON_PKG = Path(__file__).resolve().parents[1] / "services" / "common"
if "MUTANT_UNDER_TEST" not in os.environ and str(_COMMON_PKG) not in sys.path:
    sys.path.insert(0, str(_COMMON_PKG))

# Mirror of common/claims.py's defaults -- tests monkeypatch the module
# constants to these values explicitly, but minted tokens use them either way.
TEST_ISSUER = "http://keycloak:8080/realms/nexus-rag"
TEST_ISSUER_ALT = "http://localhost:8080/realms/nexus-rag"
TEST_AUDIENCE = "rag-app"

# Claim sets matching the seeded dev users in docs/dev-setup.md /
# infra/keycloak/realm-export (minus sub, which each test can override).
#
# Clearance and Releasability are carried as `rag-clearance:<value>` and
# `rag-releasability:<value>` client roles inside rag_roles, NOT as top-level
# claims -- PRs #104 and #116 moved both off free-text user attributes onto
# Keycloak client roles, so UserClaims exposes them as properties derived
# from rag_roles (common/claims.py). Setting `clearance=` or
# `releasability=` here would be silently ignored by the model.
#
# The values below mirror infra/keycloak/realm-export/nexus-rag-realm.json
# exactly, including the narrowed vocabulary from #102/#103: the
# Releasability list is NONE/NOFORN/USA/NATO/FVEY (no "REL TO ..." strings),
# and the Classification ladder is UNCLASSIFIED < CUI < SECRET (no
# "TOP SECRET"), so dave-admin tops out at SECRET.
PERSONA_CLAIMS: dict[str, dict[str, Any]] = {
    "alice-ingest": {
        "groups": ["USAREUR-AF"],
        "org": "USAREUR-AF",
        "rag_roles": ["rag-ingest", "rag-clearance:CUI", "rag-releasability:FVEY"],
    },
    "bob-query": {
        "groups": ["USAREUR-AF"],
        "org": "USAREUR-AF",
        "rag_roles": [
            "rag-query",
            "rag-clearance:SECRET",
            "rag-releasability:FVEY",
            "rag-releasability:NATO",
        ],
    },
    "carol-curator": {
        "groups": ["USAREUR-AF"],
        "org": "USAREUR-AF",
        "rag_roles": [
            "rag-query",
            "rag-curate:USAREUR-AF",
            "rag-clearance:SECRET",
            "rag-releasability:FVEY",
            "rag-releasability:NATO",
        ],
    },
    "dave-admin": {
        "groups": ["USAREUR-AF", "Signal-Corps"],
        "org": "USAREUR-AF",
        "rag_roles": [
            "rag-ingest",
            "rag-query",
            "rag-admin",
            "rag-curate:USAREUR-AF",
            "rag-curate:Signal-Corps",
            "rag-clearance:SECRET",
            "rag-releasability:NOFORN",
            "rag-releasability:USA",
            "rag-releasability:NATO",
            "rag-releasability:FVEY",
        ],
    },
}


@pytest.fixture(scope="session")
def rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_public_key(rsa_private_key):
    return rsa_private_key.public_key()


@pytest.fixture(scope="session")
def other_private_key():
    """A second keypair: tokens signed with this must never verify."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def mint_token(rsa_private_key):
    """Encode an RS256 access token with sensible defaults; any claim can be
    overridden per call (including iss/aud/exp for negative cases)."""

    def _mint(persona: str | None = None, **overrides: Any) -> str:
        payload: dict[str, Any] = {
            "sub": overrides.pop("sub", "user-0001"),
            "preferred_username": persona or "user-0001",
            # Clearance/Releasability ride in rag_roles, not as their own
            # claims -- see PERSONA_CLAIMS above.
            "groups": [],
            "org": "USAREUR-AF",
            "rag_roles": ["rag-query", "rag-clearance:SECRET", "rag-releasability:FVEY"],
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        if persona is not None:
            payload.update(PERSONA_CLAIMS[persona])
        payload.update(overrides)
        # None means "omit this claim entirely", mirroring how Keycloak drops
        # unmapped optional claims rather than emitting JSON nulls.
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, rsa_private_key, algorithm="RS256")

    return _mint
