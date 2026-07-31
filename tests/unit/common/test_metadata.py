"""Unit tests for common.metadata -- the Section 6.3 submission schema and
FR-18 server-side enforcement that uploaders can only tag within their own
clearance/releasability.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.metadata import (
    DocumentMetadataIn,
    MetadataValidationError,
    access_scope_authorized,
    validate_against_claims,
)


def _metadata(**overrides) -> DocumentMetadataIn:
    base = {
        "classification": "CUI",
        # FR-20/#102: one or more values -- Releasability became multi-value
        # and the vocabulary narrowed to NONE/NOFORN/USA/NATO/FVEY.
        "releasability": ["FVEY"],
        "access_scope": ["USAREUR-AF"],
        "source_originator": "USAREUR-AF G2",
        "doc_type": "report",
    }
    base.update(overrides)
    return DocumentMetadataIn(**base)


class TestDocumentMetadataIn:
    def test_valid_submission(self):
        meta = _metadata()
        assert meta.classification == "CUI"
        assert meta.program_community is None
        assert meta.supersedes_document_id is None

    def test_access_scope_requires_at_least_one_value(self):
        with pytest.raises(ValidationError):
            _metadata(access_scope=[])

    def test_releasability_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            _metadata(releasability=[])

    def test_releasability_accepts_multiple_values(self):
        # FR-20/Section 6.3 as of #102: one *or more* Releasability values per
        # document -- e.g. releasable to either coalition. This asserted the
        # opposite before #102 made the field multi-value.
        meta = _metadata(releasability=["NATO", "FVEY"])
        assert meta.releasability == ["NATO", "FVEY"]


class TestValidateAgainstClaims:
    def test_within_allowances_passes(self):
        validate_against_claims(
            _metadata(classification="CUI", releasability=["FVEY"]),
            allowed_classifications=["UNCLASSIFIED", "CUI", "SECRET"],
            user_releasability=["FVEY"],
        )

    def test_classification_above_clearance_rejected(self):
        with pytest.raises(MetadataValidationError) as excinfo:
            validate_against_claims(
                _metadata(classification="SECRET"),
                allowed_classifications=["UNCLASSIFIED", "CUI"],
                user_releasability=["FVEY"],
            )
        assert any("above the submitter's cleared level" in e for e in excinfo.value.errors)

    def test_unheld_releasability_rejected(self):
        with pytest.raises(MetadataValidationError) as excinfo:
            validate_against_claims(
                _metadata(releasability=["NOFORN"]),
                allowed_classifications=["UNCLASSIFIED", "CUI", "SECRET"],
                user_releasability=["FVEY"],
            )
        assert any("not held by the submitter" in e for e in excinfo.value.errors)

    def test_multiple_violations_accumulate(self):
        with pytest.raises(MetadataValidationError) as excinfo:
            validate_against_claims(
                _metadata(classification="SECRET", releasability=["NOFORN"]),
                allowed_classifications=["UNCLASSIFIED"],
                user_releasability=["FVEY"],
            )
        assert len(excinfo.value.errors) == 2


class TestAccessScopeAuthorized:
    """Issue #277: the same "any element in common" scope-matching used by
    the retrieval filter (qdrant_filters.build_access_filter), reused by the
    curation queue's scope-preference check."""

    def test_matches_on_org(self):
        assert access_scope_authorized(["USAREUR-AF"], sub="carol-sub", groups=[], org="USAREUR-AF")

    def test_matches_on_group(self):
        assert access_scope_authorized(
            ["Signal-Corps"], sub="carol-sub", groups=["Signal-Corps"], org="USAREUR-AF"
        )

    def test_matches_on_sub(self):
        assert access_scope_authorized(["carol-sub"], sub="carol-sub", groups=[], org="USAREUR-AF")

    def test_matches_on_all_authenticated(self):
        assert access_scope_authorized(
            ["ALL_AUTHENTICATED"], sub="carol-sub", groups=[], org="USAREUR-AF"
        )

    def test_no_overlap_is_not_authorized(self):
        assert not access_scope_authorized(
            ["Signal-Corps"], sub="carol-sub", groups=["Other-Unit"], org="USAREUR-AF"
        )
