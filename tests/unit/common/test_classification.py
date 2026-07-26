"""Unit tests for common.classification -- "at or below clearance" resolution
against the admin-configured ranked list (FR-18/FR-26, C9), using an
in-memory SQLite session rather than a live Postgres.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from common.classification import allowed_classifications
from common.models import ClassificationLevel

RANKS = [
    ("UNCLASSIFIED", 1),
    ("CUI", 2),
    ("SECRET", 3),
    ("TOP SECRET", 4),
]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for value, rank in RANKS:
            session.add(ClassificationLevel(value=value, rank=rank))
        session.commit()
        yield session
    engine.dispose()


class TestAllowedClassifications:
    def test_top_clearance_sees_everything(self, session):
        assert allowed_classifications(session, "TOP SECRET") == [
            "UNCLASSIFIED", "CUI", "SECRET", "TOP SECRET",
        ]

    def test_mid_clearance_sees_at_or_below(self, session):
        assert allowed_classifications(session, "SECRET") == [
            "UNCLASSIFIED", "CUI", "SECRET",
        ]

    def test_lowest_clearance_sees_only_lowest(self, session):
        assert allowed_classifications(session, "UNCLASSIFIED") == ["UNCLASSIFIED"]

    def test_unknown_clearance_sees_nothing(self, session):
        # Fail-closed: a clearance value with no configured rank must not
        # silently widen to "everything".
        assert allowed_classifications(session, "BOGUS") == []
        assert allowed_classifications(session, "") == []

    def test_inactive_levels_excluded(self, session):
        session.add(ClassificationLevel(value="CONFIDENTIAL", rank=2, active=False))
        session.commit()
        assert allowed_classifications(session, "TOP SECRET") == [
            "UNCLASSIFIED", "CUI", "SECRET", "TOP SECRET",
        ]
        # Note: the user's-own-level lookup does not filter on `active`, so an
        # inactive value still resolves as a clearance (to its active inferiors).
        # Asserting current behavior; tightening this is a deliberate follow-up.
        assert allowed_classifications(session, "CONFIDENTIAL") == [
            "UNCLASSIFIED", "CUI",
        ]
