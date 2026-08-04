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

    @pytest.mark.parametrize(
        "status", ["queued", "processing", "pending_review", "rejected", "superseded", "failed"]
    )
    def test_non_approved_target_rejected(self, status):
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(_document(status=status), **GOOD_KWARGS)
        # Exact message: FR-7 rejection text is what the submitter acts on,
        # and substring checks can't tell a garbled message from the real one
        # (#78 mutation triage -- XX-wrapped variants survived `in` asserts).
        assert excinfo.value.errors == [
            f"target document is '{status}', not 'approved' -- only an "
            "approved document can be superseded"
        ]

    def test_cross_org_target_rejected(self):
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(_document(owner_org="Signal-Corps"), **GOOD_KWARGS)
        assert any("different org" in e for e in excinfo.value.errors)

    def test_cross_org_target_short_circuits_other_checks(self):
        # Issue #325: a cross-org target must not also reveal its exact
        # status/classification/releasability -- those are only meaningful
        # to disclose to a caller with some standing over the document.
        doc = _document(
            owner_org="Signal-Corps",
            status="pending_review",
            classification="SECRET",
            releasability=["NOFORN"],
        )
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                doc, **{**GOOD_KWARGS, "allowed_classifications": ["UNCLASSIFIED", "CUI"]}
            )
        assert excinfo.value.errors == ["target document belongs to a different org"]

    def test_classification_above_clearance_rejected(self):
        # #102 narrowed the ladder to UNCLASSIFIED < CUI < SECRET, so "above
        # the submitter's cleared level" is now expressed by narrowing what
        # the submitter is allowed rather than by naming a higher level.
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                _document(classification="SECRET"),
                **{**GOOD_KWARGS, "allowed_classifications": ["UNCLASSIFIED", "CUI"]},
            )
        assert excinfo.value.errors == [
            "target document's classification is above the submitter's cleared level"
        ]

    def test_unheld_releasability_rejected(self):
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(_document(releasability=["NOFORN"]), **GOOD_KWARGS)
        assert excinfo.value.errors == [
            "submitter does not hold one or more of the target document's releasability values"
        ]

    def test_error_message_joins_all_violations(self):
        # Same contract as MetadataValidationError: str(exc) renders every
        # violation "; "-joined (#78 mutation triage).
        err = SupersedeValidationError(["first problem", "second problem"])
        assert err.errors == ["first problem", "second problem"]
        assert str(err) == "first problem; second problem"

    def test_multiple_violations_accumulate(self):
        # Same-org only -- issue #325 made a cross-org target short-circuit
        # to a single generic error instead of accumulating (see
        # test_cross_org_target_short_circuits_other_checks above), so this
        # now pins accumulation for the remaining three checks only.
        doc = _document(
            status="pending_review",
            classification="SECRET",
            releasability=["NOFORN"],
        )
        with pytest.raises(SupersedeValidationError) as excinfo:
            validate_supersede_target(
                doc, **{**GOOD_KWARGS, "allowed_classifications": ["UNCLASSIFIED", "CUI"]}
            )
        assert len(excinfo.value.errors) == 3
