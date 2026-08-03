"""Issue #343 (Phase 2 of #342): the ingestion worker's glue around the
LLM-assisted PII/sensitive-info advisory. app/pii_llm_advisory.py's HTTP
client is unit-tested in tests/test_pii_llm_advisory.py; this covers what
the worker does with its result -- merge it into
Document.tagging_advisory.pii_advisory.llm_findings alongside Phase 1's
regex findings, audit-log genuine findings, and, above all, never let any of
that break ingestion (it is decision support, not a gate). Same
technique/fixtures as test_llm_suggestion_advisory.py and
test_pii_advisory_processing.py.
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

import app.processing as processing_module
from app.pii_llm_advisory import PiiLlmFinding
from app.processing import _apply_pii_advisory, _apply_pii_llm_advisory
from common.models import AuditLogEntry, Document


class _Section:
    """Minimal stand-in for app.parsing.ParsedSection -- the advisory only
    reads `.text`."""

    def __init__(self, text: str):
        self.text = text


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


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


async def _apply(monkeypatch, session, doc, *, result, enabled=True):
    monkeypatch.setattr(processing_module, "pii_llm_enabled", lambda: enabled)

    async def _fake_suggest(text):
        return result

    monkeypatch.setattr(processing_module, "suggest_pii_llm_findings", _fake_suggest)
    await _apply_pii_llm_advisory(session, doc, [_Section("some document text")])


async def test_findings_are_recorded_and_audited(monkeypatch):
    session = _make_session()
    doc = _make_doc()
    result = [PiiLlmFinding(kind="spelled-out SSN", rationale="written out in a sentence")]

    await _apply(monkeypatch, session, doc, result=result)

    llm_findings = doc.tagging_advisory["pii_advisory"]["llm_findings"]
    assert llm_findings == [{"kind": "spelled-out SSN", "rationale": "written out in a sentence"}]
    assert _audit_count(session) == 1


async def test_empty_findings_recorded_without_audit(monkeypatch):
    session = _make_session()
    doc = _make_doc()

    await _apply(monkeypatch, session, doc, result=[])

    assert doc.tagging_advisory["pii_advisory"]["llm_findings"] == []
    assert _audit_count(session) == 0


async def test_disabled_is_a_no_op(monkeypatch):
    session = _make_session()
    doc = _make_doc()

    await _apply(monkeypatch, session, doc, result=None, enabled=False)

    assert doc.tagging_advisory is None
    assert _audit_count(session) == 0


async def test_unavailable_result_leaves_advisory_untouched(monkeypatch):
    session = _make_session()
    doc = _make_doc()
    doc.tagging_advisory = {"under_classified": False}

    await _apply(monkeypatch, session, doc, result=None)

    assert doc.tagging_advisory == {"under_classified": False}
    assert _audit_count(session) == 0


async def test_merges_alongside_regex_pii_findings_without_clobbering_them(monkeypatch):
    # Both the regex pass (#342 Phase 1) and the LLM pass (#343 Phase 2)
    # write under the same `pii_advisory` key -- running one must not erase
    # what the other already wrote.
    session = _make_session()
    doc = _make_doc()
    sections = [_Section("Employee SSN on file: 234-56-7890. Also written as two three four...")]

    _apply_pii_advisory(session, doc, sections)
    result = [PiiLlmFinding(kind="spelled-out SSN", rationale="spelled out later in the text")]
    monkeypatch.setattr(processing_module, "pii_llm_enabled", lambda: True)

    async def _fake_suggest(text):
        return result

    monkeypatch.setattr(processing_module, "suggest_pii_llm_findings", _fake_suggest)
    await _apply_pii_llm_advisory(session, doc, sections)

    assert doc.tagging_advisory["pii_advisory"]["findings"][0]["kind"] == "ssn"
    assert doc.tagging_advisory["pii_advisory"]["llm_findings"] == [
        {"kind": "spelled-out SSN", "rationale": "spelled out later in the text"}
    ]


async def test_merges_with_other_advisory_keys_without_clobbering_them(monkeypatch):
    session = _make_session()
    doc = _make_doc()
    doc.tagging_advisory = {
        "under_classified": True,
        "precedent": {"disagrees_with_assigned": False},
    }
    result = [PiiLlmFinding(kind="foreign national ID", rationale=None)]

    await _apply(monkeypatch, session, doc, result=result)

    assert doc.tagging_advisory["under_classified"] is True
    assert doc.tagging_advisory["precedent"] == {"disagrees_with_assigned": False}
    assert doc.tagging_advisory["pii_advisory"]["llm_findings"] == [
        {"kind": "foreign national ID", "rationale": None}
    ]


async def test_client_failure_is_swallowed_and_leaves_advisory_untouched(monkeypatch):
    session = _make_session()
    doc = _make_doc()
    monkeypatch.setattr(processing_module, "pii_llm_enabled", lambda: True)

    async def _raise(text):
        raise RuntimeError("model host unreachable")

    monkeypatch.setattr(processing_module, "suggest_pii_llm_findings", _raise)

    await _apply_pii_llm_advisory(session, doc, [_Section("text")])

    assert doc.tagging_advisory is None
    assert _audit_count(session) == 0


async def test_audit_entry_never_echoes_raw_text_beyond_model_output(monkeypatch):
    session = _make_session()
    doc = _make_doc()
    result = [PiiLlmFinding(kind="spelled-out SSN", rationale="see the finding kind")]

    await _apply(monkeypatch, session, doc, result=result)

    entry = session.exec(
        select(AuditLogEntry).where(AuditLogEntry.action == "document.tagging_advisory")
    ).one()
    assert entry.detail == {
        "pii_advisory": {
            "llm_findings": [{"kind": "spelled-out SSN", "rationale": "see the finding kind"}]
        }
    }
