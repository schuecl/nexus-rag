"""#160: the Milvus FR-26 access expression must encode exactly the semantics
of qdrant_filters.build_access_filter -- clause for clause. These tests mirror
test_qdrant_filters.py's cases against the expression builder, plus the
injection cases an expression language adds (a hostile group name must not be
able to terminate a string literal and widen the filter)."""

from __future__ import annotations

import pytest

from common.claims import UserClaims
from common.metadata import ALL_AUTHENTICATED_ACCESS_SCOPE, NO_RELEASABILITY_RESTRICTION
from common.milvus_store import build_access_expr
from common.vector_store import get_store


def _claims(*, clearance="SECRET", releasability=("FVEY",), **overrides) -> UserClaims:
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


class TestStatusClause:
    def test_status_is_always_approved(self):
        expr = build_access_expr(_claims(), allowed_classifications=ALLOWED)
        assert 'status == "approved"' in expr

    def test_status_approved_regardless_of_claims(self):
        for clearance in ("", "UNCLASSIFIED", "TOP SECRET"):
            expr = build_access_expr(_claims(clearance=clearance), allowed_classifications=ALLOWED)
            assert 'status == "approved"' in expr


class TestClassificationClause:
    def test_classification_limited_to_allowed_list(self):
        expr = build_access_expr(_claims(), allowed_classifications=ALLOWED)
        assert 'classification in ["UNCLASSIFIED", "CUI", "SECRET"]' in expr

    def test_empty_allowed_list_matches_nothing(self):
        # Fail-closed: an unknown clearance yields an empty allowed list and
        # the clause must not degenerate into match-anything.
        expr = build_access_expr(_claims(clearance=""), allowed_classifications=[])
        assert "classification in []" in expr


class TestReleasabilityClause:
    def test_none_restriction_is_always_included(self):
        expr = build_access_expr(_claims(releasability=()), allowed_classifications=ALLOWED)
        assert f'array_contains_any(releasability, ["{NO_RELEASABILITY_RESTRICTION}"])' in expr

    def test_user_holdings_are_added(self):
        expr = build_access_expr(
            _claims(releasability=("FVEY", "NATO")), allowed_classifications=ALLOWED
        )
        assert (
            f'array_contains_any(releasability, ["{NO_RELEASABILITY_RESTRICTION}", '
            f'"FVEY", "NATO"])' in expr
        )


class TestAccessScopeClause:
    def test_scope_covers_all_authenticated_sub_groups_and_org(self):
        expr = build_access_expr(_claims(), allowed_classifications=ALLOWED)
        for value in (
            ALL_AUTHENTICATED_ACCESS_SCOPE,
            "user-0001",
            "analysts",
            "USAREUR-AF",
        ):
            assert f'"{value}"' in expr.split("array_contains_any(access_scope,")[1]

    def test_missing_org_is_omitted(self):
        expr = build_access_expr(_claims(org=None), allowed_classifications=ALLOWED)
        scope_part = expr.split("array_contains_any(access_scope,")[1]
        assert "USAREUR-AF" not in scope_part


class TestInjectionResistance:
    def test_quote_in_group_name_cannot_break_out(self):
        hostile = 'x" or status != "'
        expr = build_access_expr(_claims(groups=[hostile]), allowed_classifications=ALLOWED)
        # The hostile value appears exactly once, as a single escaped string
        # literal -- its quotes escaped, so a parser reads it as data.
        assert '"x\\" or status != \\""' in expr
        # And no *unescaped* occurrence exists anywhere (an unescaped quote is
        # what would terminate the literal and let the rest execute as expr).
        assert '"x" or' not in expr

    def test_backslash_is_escaped_before_quote(self):
        expr = build_access_expr(_claims(groups=['trailing\\"']), allowed_classifications=ALLOWED)
        assert '\\\\\\"' in expr


class TestBackendSelection:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        get_store.cache_clear()
        yield
        get_store.cache_clear()

    def test_default_is_qdrant(self, monkeypatch):
        monkeypatch.delenv("VECTOR_BACKEND", raising=False)
        assert type(get_store()).__name__ == "QdrantStore"

    def test_milvus_is_selectable(self, monkeypatch):
        monkeypatch.setenv("VECTOR_BACKEND", "milvus")
        assert type(get_store()).__name__ == "MilvusStore"

    def test_unknown_backend_refuses_to_guess(self, monkeypatch):
        monkeypatch.setenv("VECTOR_BACKEND", "weaviate")
        with pytest.raises(ValueError, match="refusing to guess"):
            get_store()
