"""Unit tests for common.claims -- OIDC token verification and the UserClaims
authorization properties that both ingestion-time tagging (FR-18) and
query-time filtering (FR-26) derive from.
"""

from __future__ import annotations

from types import SimpleNamespace

import jwt as pyjwt
import pytest

from common import claims as claims_module
from common.claims import UserClaims, parse_claims


class _FakeJWKClient:
    """Stands in for jwt.PyJWKClient: always returns the test RSA public key,
    no network fetch to a real JWKS endpoint."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=self._public_key)


@pytest.fixture
def verified_env(monkeypatch, rsa_public_key):
    """Point claims parsing at the test keypair and issuer/audience constants."""
    monkeypatch.setattr(claims_module, "OIDC_ISSUERS", ["http://keycloak:8080/realms/nexus-rag",
                                                        "http://localhost:8080/realms/nexus-rag"])
    monkeypatch.setattr(claims_module, "OIDC_AUDIENCE", "rag-app")
    monkeypatch.setattr(claims_module, "OIDC_SKIP_VERIFY", False)
    monkeypatch.setattr(
        claims_module, "_jwk_client", lambda: _FakeJWKClient(rsa_public_key)
    )


class TestParseClaims:
    def test_valid_token_yields_all_claims(self, verified_env, mint_token):
        token = mint_token(persona="carol-curator", sub="carol-sub")
        parsed = parse_claims(token)
        assert parsed.sub == "carol-sub"
        assert parsed.preferred_username == "carol-curator"
        # clearance/releasability are derived from rag-clearance:/
        # rag-releasability: client roles (#104, #116), not standalone claims.
        assert parsed.clearance == "SECRET"
        assert parsed.releasability == ["FVEY", "NATO"]
        assert parsed.org == "USAREUR-AF"
        assert parsed.rag_roles == [
            "rag-query",
            "rag-curate:USAREUR-AF",
            "rag-clearance:SECRET",
            "rag-releasability:FVEY",
            "rag-releasability:NATO",
        ]

    def test_bearer_prefix_is_stripped(self, verified_env, mint_token):
        token = mint_token(persona="bob-query")
        assert parse_claims(f"Bearer {token}").preferred_username == "bob-query"

    def test_alternate_issuer_accepted(self, verified_env, mint_token):
        # The dev stack issues tokens under both hostnames (see claims.py's
        # OIDC_ISSUERS comment); both must validate.
        token = mint_token(iss="http://localhost:8080/realms/nexus-rag")
        assert parse_claims(token).sub == "user-0001"

    def test_expired_token_rejected(self, verified_env, mint_token):
        token = mint_token(exp=1)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            parse_claims(token)

    def test_wrong_issuer_rejected(self, verified_env, mint_token):
        token = mint_token(iss="http://attacker.example/realms/nexus-rag")
        with pytest.raises(pyjwt.InvalidIssuerError):
            parse_claims(token)

    def test_wrong_audience_rejected(self, verified_env, mint_token):
        token = mint_token(aud="some-other-client")
        with pytest.raises(pyjwt.InvalidAudienceError):
            parse_claims(token)

    def test_foreign_signature_rejected(self, verified_env, mint_token, other_private_key):
        token = mint_token()
        forged = pyjwt.encode(
            pyjwt.decode(token, options={"verify_signature": False}),
            other_private_key,
            algorithm="RS256",
        )
        with pytest.raises(pyjwt.InvalidSignatureError):
            parse_claims(forged)

    def test_missing_optional_claims_default(self, verified_env, mint_token):
        # Keycloak omits unmapped optional claims; defaults must kick in.
        token = mint_token(clearance=None, releasability=None, groups=None,
                           org=None, rag_roles=None)
        parsed = parse_claims(token)
        assert parsed.clearance == ""
        assert parsed.releasability == []
        assert parsed.groups == []
        assert parsed.org is None
        assert parsed.rag_roles == []

    def test_skip_verify_decodes_without_signature_check(self, monkeypatch, mint_token):
        monkeypatch.setattr(claims_module, "OIDC_SKIP_VERIFY", True)
        token = mint_token(persona="bob-query")
        parsed = parse_claims(token)
        assert parsed.preferred_username == "bob-query"


class TestUserClaimsProperties:
    def test_can_ingest_and_query(self):
        assert UserClaims(sub="u", preferred_username="u",
                          rag_roles=["rag-ingest"]).can_ingest
        assert not UserClaims(sub="u", preferred_username="u",
                              rag_roles=["rag-query"]).can_ingest
        assert UserClaims(sub="u", preferred_username="u",
                          rag_roles=["rag-query"]).can_query

    def test_curatable_orgs_extracts_prefixed_roles(self):
        claims = UserClaims(
            sub="d", preferred_username="dave-admin",
            rag_roles=["rag-ingest", "rag-query",
                       "rag-curate:USAREUR-AF", "rag-curate:Signal-Corps"],
        )
        assert sorted(claims.curatable_orgs) == ["Signal-Corps", "USAREUR-AF"]
        assert claims.can_curate_org("USAREUR-AF")
        assert claims.can_curate_org("Signal-Corps")
        assert not claims.can_curate_org("OTHER-ORG")

    def test_curate_role_does_not_partial_match(self):
        claims = UserClaims(sub="c", preferred_username="c",
                            rag_roles=["rag-curate:USAREUR-AF"])
        # Exact-org match only -- no prefix/suffix bleed between org names.
        assert not claims.can_curate_org("USAREUR")
        assert not claims.can_curate_org("USAREUR-AF-East")

    def test_no_roles_means_no_privileges(self):
        claims = UserClaims(sub="x", preferred_username="x")
        assert not claims.can_ingest
        assert not claims.can_query
        assert claims.curatable_orgs == []
