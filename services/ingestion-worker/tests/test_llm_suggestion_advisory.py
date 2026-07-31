"""Issue #308 (Phase 3 of #138): the ingestion worker's glue around the LLM
zero-shot classification suggestion. app/classification_suggestion.py's HTTP
client is unit-tested in tests/test_classification_suggestion.py; this covers
what the worker does with its result -- fold it into doc.tagging_advisory
alongside Phase 1/2's findings, audit-log a genuine disagreement, and, above
all, never let any of that break ingestion (it is decision-support, not a
gate), same posture as test_precedent_advisory.py.
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

import app.processing as processing_module
from app.classification_suggestion import ClassificationSuggestion
from app.parsing import ParsedSection
from app.processing import _apply_llm_suggestion_advisory
from common.models import AuditLogEntry, ClassificationLevel, Document

RANKS = [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3)]


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    for value, rank in RANKS:
        session.add(ClassificationLevel(value=value, rank=rank))
    session.commit()
    return session


def _make_doc(classification: str, doc_type: str = "report") -> Document:
    return Document(
        filename="report.txt",
        uploader_sub="user-1",
        uploader_username="alice",
        owner_org="USAREUR-AF",
        classification=classification,
        releasability=["NONE"],
        access_scope=["ALL_AUTHENTICATED"],
        source_originator="USAREUR-AF",
        doc_type=doc_type,
    )


def _sections(text: str = "some document text") -> list[ParsedSection]:
    return [ParsedSection(text=text)]


def _audit_count(session: Session) -> int:
    return len(
        session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.tagging_advisory")
        ).all()
    )


async def _apply(monkeypatch, session, doc, *, result, enabled=True):
    monkeypatch.setattr(processing_module, "suggestion_enabled", lambda: enabled)

    async def _fake_suggest(text, *, classifications):
        return result

    monkeypatch.setattr(processing_module, "suggest_classification", _fake_suggest)
    await _apply_llm_suggestion_advisory(session, doc, _sections())


async def test_higher_suggested_classification_is_flagged_and_audited(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    result = ClassificationSuggestion(
        classification="SECRET",
        doc_type="report",
        program_community=None,
        confidence=0.9,
        rationale="mentions classified operations",
    )

    await _apply(monkeypatch, session, doc, result=result)

    llm = doc.tagging_advisory["llm_suggestion"]
    assert llm["disagrees_with_assigned"] is True
    assert llm["suggested_classification"] == "SECRET"
    assert llm["confidence"] == 0.9
    assert _audit_count(session) == 1


async def test_matching_suggestion_is_not_flagged(monkeypatch):
    session = _make_session()
    doc = _make_doc("SECRET")
    result = ClassificationSuggestion(
        classification="SECRET",
        doc_type="report",
        program_community=None,
        confidence=0.7,
        rationale=None,
    )

    await _apply(monkeypatch, session, doc, result=result)

    llm = doc.tagging_advisory["llm_suggestion"]
    assert llm["disagrees_with_assigned"] is False
    assert _audit_count(session) == 0


async def test_lower_suggested_classification_is_not_flagged(monkeypatch):
    # Same asymmetric semantics as Phase 1/2: only an under-classification
    # signal (suggested higher than assigned) is worth a curator's attention.
    session = _make_session()
    doc = _make_doc("SECRET")
    result = ClassificationSuggestion(
        classification="CUI",
        doc_type="report",
        program_community=None,
        confidence=0.6,
        rationale=None,
    )

    await _apply(monkeypatch, session, doc, result=result)

    llm = doc.tagging_advisory["llm_suggestion"]
    assert llm["disagrees_with_assigned"] is False
    assert _audit_count(session) == 0


async def test_differing_doc_type_is_flagged_regardless_of_classification(monkeypatch):
    session = _make_session()
    doc = _make_doc("SECRET", doc_type="report")
    result = ClassificationSuggestion(
        classification="SECRET",
        doc_type="briefing slide",
        program_community=None,
        confidence=0.8,
        rationale=None,
    )

    await _apply(monkeypatch, session, doc, result=result)

    llm = doc.tagging_advisory["llm_suggestion"]
    assert llm["disagrees_with_assigned"] is True
    assert _audit_count(session) == 1


async def test_doc_type_comparison_is_case_insensitive(monkeypatch):
    session = _make_session()
    doc = _make_doc("SECRET", doc_type="Report")
    result = ClassificationSuggestion(
        classification="SECRET",
        doc_type="report",
        program_community=None,
        confidence=0.8,
        rationale=None,
    )

    await _apply(monkeypatch, session, doc, result=result)

    llm = doc.tagging_advisory["llm_suggestion"]
    assert llm["disagrees_with_assigned"] is False


async def test_disabled_is_a_no_op(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")

    await _apply(monkeypatch, session, doc, result=None, enabled=False)

    assert doc.tagging_advisory is None
    assert _audit_count(session) == 0


async def test_no_suggestion_leaves_advisory_untouched(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    doc.tagging_advisory = {"under_classified": False}

    await _apply(monkeypatch, session, doc, result=None)

    assert doc.tagging_advisory == {"under_classified": False}
    assert _audit_count(session) == 0


async def test_merges_with_existing_advisory_without_clobbering_it(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    doc.tagging_advisory = {
        "under_classified": True,
        "detected_classification": "SECRET",
        "precedent": {"disagrees_with_assigned": False},
    }
    result = ClassificationSuggestion(
        classification="SECRET",
        doc_type="report",
        program_community=None,
        confidence=0.9,
        rationale=None,
    )

    await _apply(monkeypatch, session, doc, result=result)

    assert doc.tagging_advisory["under_classified"] is True
    assert doc.tagging_advisory["detected_classification"] == "SECRET"
    assert doc.tagging_advisory["precedent"] == {"disagrees_with_assigned": False}
    assert "llm_suggestion" in doc.tagging_advisory


async def test_client_failure_is_swallowed_and_leaves_advisory_untouched(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    monkeypatch.setattr(processing_module, "suggestion_enabled", lambda: True)

    async def _raise(text, *, classifications):
        raise RuntimeError("model host unreachable")

    monkeypatch.setattr(processing_module, "suggest_classification", _raise)

    await _apply_llm_suggestion_advisory(session, doc, _sections())

    assert doc.tagging_advisory is None
    assert _audit_count(session) == 0


async def test_unranked_suggested_classification_is_not_flagged(monkeypatch):
    # A suggested value absent from the configured ClassificationLevel list
    # can't be ranked against the assigned tag -- fail closed on the
    # comparison (no finding), not on the whole advisory. (Shouldn't happen
    # in practice since the client already validates against the configured
    # list, but the worker-side comparison is defensive on its own.)
    session = _make_session()
    doc = _make_doc("CUI")
    result = ClassificationSuggestion(
        classification="UNKNOWN-LEVEL",
        doc_type="report",
        program_community=None,
        confidence=0.5,
        rationale=None,
    )

    await _apply(monkeypatch, session, doc, result=result)

    llm = doc.tagging_advisory["llm_suggestion"]
    assert llm["disagrees_with_assigned"] is False
