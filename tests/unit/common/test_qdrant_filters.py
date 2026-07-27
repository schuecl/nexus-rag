"""Unit tests for common.qdrant_filters -- the FR-26 access filter. This is
the single most security-critical pure function in the repo: if it ever
widens scope, unauthorized content leaks into retrieval results.
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, MatchAny, MatchValue

from common.claims import UserClaims
from common.metadata import ALL_AUTHENTICATED_ACCESS_SCOPE, NO_RELEASABILITY_RESTRICTION
from common.qdrant_filters import build_access_filter


def _conditions_by_key(qfilter) -> dict[str, FieldCondition]:
    return {cond.key: cond for cond in qfilter.must}


def _claims(*, clearance="SECRET", releasability=("FVEY",), **overrides) -> UserClaims:
    """clearance/releasability are properties on UserClaims, derived from
    rag-clearance:/rag-releasability: client roles (#104, #116) -- passing
    them as fields would be silently ignored, so this builds the roles."""
    roles = ["rag-query"]
    if clearance:
        roles.append(f"rag-clearance:{clearance}")
    roles += [f"rag-releasability:{v}" for v in releasability]
    base = {
        "sub": "user-0001",
        "preferred_username": "user-0001",
        "groups": ["analysts"],
        "org": "USAREUR-AF",
        "rag_roles": roles,
    }
    base.update(overrides)
    return UserClaims(**base)


ALLOWED = ["UNCLASSIFIED", "CUI", "SECRET"]


class TestStatusCondition:
    def test_status_is_always_approved(self):
        qfilter = build_access_filter(_claims(), allowed_classifications=ALLOWED)
        status = _conditions_by_key(qfilter)["status"]
        assert isinstance(status.match, MatchValue)
        assert status.match.value == "approved"

    def test_status_approved_regardless_of_claims(self):
        # No claim combination (even admin-level) may unlock non-approved docs.
        for clearance in ("", "UNCLASSIFIED", "TOP SECRET"):
            qfilter = build_access_filter(
                _claims(clearance=clearance), allowed_classifications=ALLOWED
            )
            assert _conditions_by_key(qfilter)["status"].match.value == "approved"


class TestClassificationCondition:
    def test_classification_matches_allowed_list(self):
        qfilter = build_access_filter(_claims(), allowed_classifications=ALLOWED)
        cond = _conditions_by_key(qfilter)["classification"]
        assert isinstance(cond.match, MatchAny)
        assert cond.match.any == ALLOWED

    def test_empty_allowed_list_admits_nothing(self):
        # Unknown clearance resolves to [] upstream (common/classification.py);
        # the filter must then match no document rather than everything.
        qfilter = build_access_filter(_claims(clearance="BOGUS"), allowed_classifications=[])
        assert _conditions_by_key(qfilter)["classification"].match.any == []


class TestReleasabilityCondition:
    def test_releasability_matches_user_values(self):
        qfilter = build_access_filter(
            _claims(releasability=["NATO", "FVEY"]),
            allowed_classifications=ALLOWED,
        )
        cond = _conditions_by_key(qfilter)["releasability"]
        # NONE is always admitted alongside the user's own holdings: it is the
        # explicit "no coalition caveat" value (common/metadata.py), not a
        # caveat anyone needs to hold.
        assert cond.match.any == [NO_RELEASABILITY_RESTRICTION, "NATO", "FVEY"]

    def test_no_holdings_still_admits_only_uncaveated_documents(self):
        # A user holding no Releasability roles sees documents tagged NONE and
        # nothing else -- it must never degrade into "no constraint", and it
        # must not become "match nothing" either, since NONE is the normal
        # state for most documents.
        qfilter = build_access_filter(_claims(releasability=[]), allowed_classifications=ALLOWED)
        cond = _conditions_by_key(qfilter)["releasability"]
        assert cond.match.any == [NO_RELEASABILITY_RESTRICTION]


class TestAccessScopeCondition:
    def test_scope_includes_all_authenticated_sub_and_groups(self):
        qfilter = build_access_filter(_claims(), allowed_classifications=ALLOWED)
        scope = _conditions_by_key(qfilter)["access_scope"].match.any
        assert ALL_AUTHENTICATED_ACCESS_SCOPE in scope
        assert "user-0001" in scope
        assert "analysts" in scope

    def test_org_included_when_present(self):
        qfilter = build_access_filter(_claims(org="USAREUR-AF"), allowed_classifications=ALLOWED)
        assert "USAREUR-AF" in _conditions_by_key(qfilter)["access_scope"].match.any

    def test_org_absent_when_none(self):
        qfilter = build_access_filter(_claims(org=None), allowed_classifications=ALLOWED)
        scope = _conditions_by_key(qfilter)["access_scope"].match.any
        assert "USAREUR-AF" not in scope
        # ...but the always-on scopes survive.
        assert ALL_AUTHENTICATED_ACCESS_SCOPE in scope
        assert "user-0001" in scope

    def test_no_foreign_values_leak_into_scope(self):
        qfilter = build_access_filter(_claims(), allowed_classifications=ALLOWED)
        scope = set(_conditions_by_key(qfilter)["access_scope"].match.any)
        assert scope == {ALL_AUTHENTICATED_ACCESS_SCOPE, "user-0001", "analysts", "USAREUR-AF"}

    def test_distinct_users_get_distinct_scopes(self):
        scope_a = _conditions_by_key(
            build_access_filter(_claims(sub="alice"), allowed_classifications=ALLOWED)
        )["access_scope"].match.any
        scope_b = _conditions_by_key(
            build_access_filter(_claims(sub="mallory", groups=[]), allowed_classifications=ALLOWED)
        )["access_scope"].match.any
        assert "mallory" not in scope_a
        assert "alice" not in scope_b
        assert "analysts" not in scope_b
