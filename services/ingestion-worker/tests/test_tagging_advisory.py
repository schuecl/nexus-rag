"""Issue #138 Phase 1: the ingestion worker's glue around the advisory
marking-mismatch guardrail (common/marking_detection.py). The detection and
comparison logic is unit-tested in tests/unit/common/test_marking_detection.py;
this covers what the worker does with it -- attach the advisory to the Document,
audit-log genuine findings, and, above all, never let any of that break
ingestion (it is decision-support, not a gate).
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from app.parsing import OcrStatus
from app.processing import _apply_tagging_advisory
from common.models import AuditLogEntry, ClassificationLevel, Document, ReleasabilityValue

RANKS = [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3)]
RELEASABILITY = ["NONE", "NOFORN", "USA", "NATO", "FVEY"]


class _Section:
    """Minimal stand-in for app.parsing.ParsedSection -- the advisory only
    reads `.text`."""

    def __init__(self, text: str):
        self.text = text


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    for value, rank in RANKS:
        session.add(ClassificationLevel(value=value, rank=rank))
    for value in RELEASABILITY:
        session.add(ReleasabilityValue(value=value))
    session.commit()
    return session


def _make_doc(classification: str, releasability: list[str]) -> Document:
    return Document(
        filename="report.txt",
        uploader_sub="user-1",
        uploader_username="alice",
        owner_org="USAREUR-AF",
        classification=classification,
        releasability=releasability,
        access_scope=["ALL_AUTHENTICATED"],
        source_originator="USAREUR-AF",
        doc_type="report",
    )


def _audit_count(session: Session) -> int:
    return len(
        session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.tagging_advisory")
        ).all()
    )


def test_under_classification_is_flagged_and_audited():
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])
    sections = [_Section("SECRET//NOFORN"), _Section("Body text of the report.")]

    _apply_tagging_advisory(session, doc, sections)

    assert doc.tagging_advisory is not None
    assert doc.tagging_advisory["under_classified"] is True
    assert doc.tagging_advisory["detected_classification"] == "SECRET"
    # A genuine finding is recorded for the curator/audit trail.
    assert _audit_count(session) == 1


def test_clean_document_records_advisory_but_no_finding_and_no_audit():
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])
    sections = [_Section("Ordinary prose with no classification markings at all.")]

    _apply_tagging_advisory(session, doc, sections)

    assert doc.tagging_advisory is not None
    assert doc.tagging_advisory["under_classified"] is False
    # No finding -> nothing to audit.
    assert _audit_count(session) == 0


def test_correctly_tagged_document_not_flagged():
    session = _make_session()
    doc = _make_doc("SECRET", ["NOFORN"])
    sections = [_Section("SECRET//NOFORN"), _Section("content")]

    _apply_tagging_advisory(session, doc, sections)

    assert doc.tagging_advisory["under_classified"] is False
    assert doc.tagging_advisory["unassigned_caveats"] == []
    assert _audit_count(session) == 0


def test_unknown_marking_segment_not_flagged_but_configured_caveat_is():
    # PR #2 review: SP-CTI is a CUI control marking, not a releasability value
    # -- with the configured ReleasabilityValue vocabulary passed through, it
    # must not be reported as missing releasability, while NOFORN (configured,
    # unassigned) still is.
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])
    sections = [_Section("CUI//SP-CTI//NOFORN"), _Section("content")]

    _apply_tagging_advisory(session, doc, sections)

    assert doc.tagging_advisory["unassigned_caveats"] == ["NOFORN"]
    assert set(doc.tagging_advisory["detected_caveats"]) == {"SP-CTI", "NOFORN"}
    assert _audit_count(session) == 1


def test_ocr_skip_flags_markings_not_scanned_and_is_audited():
    # Issue #306 gap 3: a document whose scanned pages couldn't be OCR'd must
    # not look identical to a clean document -- even with zero marking
    # findings, the curator needs to know coverage was incomplete.
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])
    sections = [_Section("Ordinary prose with no classification markings at all.")]
    ocr_status = OcrStatus()
    ocr_status.record("ocr_unavailable")

    _apply_tagging_advisory(session, doc, sections, ocr_status)

    assert doc.tagging_advisory["under_classified"] is False
    assert doc.tagging_advisory["markings_not_scanned"] is True
    assert doc.tagging_advisory["unscanned_reasons"] == ["ocr_unavailable"]
    # Unlike a plain clean document, this is still audited -- a curator
    # reviewing the queue must be able to see the coverage gap.
    assert _audit_count(session) == 1


def test_no_ocr_skip_leaves_markings_not_scanned_false():
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])
    sections = [_Section("Ordinary prose with no classification markings at all.")]
    ocr_status = OcrStatus()

    _apply_tagging_advisory(session, doc, sections, ocr_status)

    assert doc.tagging_advisory["markings_not_scanned"] is False
    assert doc.tagging_advisory["unscanned_reasons"] == []
    assert _audit_count(session) == 0


def test_ocr_status_defaults_to_none_without_breaking_advisory():
    # Callers that don't track OCR coverage (none today besides the worker's
    # own process_document) can omit ocr_status entirely.
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])
    sections = [_Section("Ordinary prose with no classification markings at all.")]

    _apply_tagging_advisory(session, doc, sections)

    assert doc.tagging_advisory["markings_not_scanned"] is False


def test_failure_is_swallowed_and_leaves_advisory_null():
    session = _make_session()
    doc = _make_doc("CUI", ["NONE"])

    class _Exploding:
        @property
        def text(self):
            raise RuntimeError("boom")

    # Must not raise, and must leave the document's advisory untouched so the
    # pipeline proceeds exactly as if the guardrail weren't there.
    _apply_tagging_advisory(session, doc, [_Exploding()])

    assert doc.tagging_advisory is None
    assert _audit_count(session) == 0
