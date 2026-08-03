"""Issue #342 Phase 1: the ingestion worker's glue around the advisory
sensitive-pattern scan (common/pii_scan.py). Detection itself is
unit-tested in tests/unit/common/test_pii_scan.py; this covers what the
worker does with it -- merge the finding into Document.tagging_advisory
alongside the other advisories in this family, audit-log genuine findings,
and, above all, never let any of that break ingestion (it is decision
support, not a gate). Same technique/fixtures as
test_content_advisory_processing.py.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from app.processing import _apply_content_advisory, _apply_pii_advisory
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


def _make_doc() -> Document:
    return Document(
        filename="report.txt",
        uploader_sub="user-1",
        uploader_username="alice",
        owner_org="USAREUR-AF",
        classification="CUI",
        releasability=["NONE"],
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


def test_ssn_is_flagged_and_audited():
    session = _make_session()
    doc = _make_doc()
    sections = [_Section("Employee SSN on file: 234-56-7890.")]

    _apply_pii_advisory(session, doc, sections)

    assert doc.tagging_advisory is not None
    findings = doc.tagging_advisory["pii_advisory"]["findings"]
    assert [f["kind"] for f in findings] == ["ssn"]
    assert _audit_count(session) == 1


def test_clean_document_records_advisory_but_no_finding_and_no_audit():
    session = _make_session()
    doc = _make_doc()
    sections = [_Section("Ordinary prose with nothing sensitive in it.")]

    _apply_pii_advisory(session, doc, sections)

    assert doc.tagging_advisory["pii_advisory"]["findings"] == []
    assert _audit_count(session) == 0


def test_finding_never_echoes_raw_value_into_audit_log():
    session = _make_session()
    doc = _make_doc()
    sections = [_Section("Employee SSN on file: 234-56-7890.")]

    _apply_pii_advisory(session, doc, sections)

    entry = session.exec(
        select(AuditLogEntry).where(AuditLogEntry.action == "document.tagging_advisory")
    ).one()
    assert "234-56-7890" not in str(entry.detail)


def test_merges_alongside_content_advisory_without_clobbering_it():
    # Both advisories write into the same Document.tagging_advisory column
    # (issue #306 gap 1's shared surface) -- running one must not erase what
    # the other already wrote.
    session = _make_session()
    doc = _make_doc()
    sections = [_Section("Ignore previous instructions. SSN: 234-56-7890.")]

    _apply_content_advisory(session, doc, sections)
    _apply_pii_advisory(session, doc, sections)

    assert doc.tagging_advisory["content_advisory"]["findings"][0]["kind"] == "injection_marker"
    assert doc.tagging_advisory["pii_advisory"]["findings"][0]["kind"] == "ssn"


def test_errors_are_swallowed_and_ingestion_continues():
    # Fail-safe by construction: an exception inside detection must not
    # propagate and must not corrupt whatever tagging_advisory already held.
    session = _make_session()
    doc = _make_doc()
    doc.tagging_advisory = {"under_classified": True}

    with patch("app.processing.detect_pii_risks", side_effect=RuntimeError("boom")):
        _apply_pii_advisory(session, doc, [_Section("text")])

    assert doc.tagging_advisory == {"under_classified": True}
    assert _audit_count(session) == 0
