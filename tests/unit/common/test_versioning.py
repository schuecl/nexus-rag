"""Unit tests for common.versioning -- FR-7 supersede-target authorization.
These guards are what stop an uploader from naming an arbitrary document id
and having its vectors silently deleted at approval time.
"""

from __future__ import annotations

import uuid

import pytest

from common.models import Document
from common.versioning import SupersedeValidationError, validate_supersede_target


def _document(**overrides) -> Document:
    base = {
        "id": uuid.uuid4(),
        "filename": "old-report.pdf",
        "uploader_sub": "someone-else",
        "uploader_username": "someone-else",
        "owner_org": "USAREUR-AF",
        "classification": "CUI",
        "releasability": ["FVEY"],
        "access_scope": ["USAREUR-AF"],
        "source_originator": "USAREUR-AF G2",
        "doc_type": "report",
        "status": "approved",
    }
    base.update(overrides)
    return Document(**base)


GOOD_KWARGS = {
    "new_owner_org": "USAREUR-AF",
    "allowed_classifications": ["UNCLASSIFIED", "CUI", "SECRET"],
    "user_releasability": ["FVEY"],
}


class TestValidateSupersedeTarget:
    def test_valid_target_passes(self):
        validate_supersede_target(_document(), **GOOD_KWARGS)

    @pytest.mark.parametrize("status", ["queued", "processing", "pending_review",
                                        "rejected", "superseded", "failed"])
    def test_non_approved_target_rejected(self, status):
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(_document(status=status), **GOOD_KWARGS)
        assert any("not 'approved'" in e for e in excinfo.value.errors)

    def test_cross_org_target_rejected(self):
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                _document(owner_org="Signal-Corps"), **GOOD_KWARGS
            )
        assert any("different org" in e for e in excinfo.value.errors)

    def test_classification_above_clearance_rejected(self):
        # #102 narrowed the ladder to UNCLASSIFIED < CUI < SECRET, so "above
        # the submitter's cleared level" is now expressed by narrowing what
        # the submitter is allowed rather than by naming a higher level.
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                _document(classification="SECRET"),
                **{**GOOD_KWARGS, "allowed_classifications": ["UNCLASSIFIED", "CUI"]},
            )
        assert any("above the submitter's cleared level" in e
                   for e in excinfo.value.errors)

    def test_unheld_releasability_rejected(self):
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                _document(releasability=["NOFORN"]), **GOOD_KWARGS
            )
        assert any("does not hold" in e for e in excinfo.value.errors)

    def test_multiple_violations_accumulate(self):
        doc = _document(status="pending_review", owner_org="Signal-Corps",
                        classification="SECRET", releasability=["NOFORN"])
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                doc, **{**GOOD_KWARGS, "allowed_classifications": ["UNCLASSIFIED", "CUI"]}
            )
        assert len(excinfo.value.errors) == 4
