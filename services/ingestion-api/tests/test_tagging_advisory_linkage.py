"""Issue #306 gap 1: approve()/reject() must tie their decision back to the
ingestion-time marking-mismatch advisory (issue #138) when one was flagged,
rather than leaving "did the curator agree with the flag" as something only
recoverable by diffing the `document.tagging_advisory` and `document.approve`/
`document.reject` audit rows by document ID after the fact.

Deliberately reads only `doc.tagging_advisory` (a Document column), never
audit_log itself -- issue #278's per-service DB grants make audit_log
INSERT-only for every application role, including ingestion-api's, and a
`select(AuditLogEntry)` here would violate that (caught live against the real
dev stack: it raised `psycopg.errors.InsufficientPrivilege`, which this
in-memory-SQLite suite can't reproduce since SQLite has no such grants).

Same technique as test_curate_nfr13_revert.py: call approve()/reject()
directly against an in-memory SQLite session, bypassing the FastAPI layer.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.routes import curate
from common.claims import UserClaims
from common.models import AuditLogEntry, ClassificationLevel, Document

CURATOR = UserClaims(
    sub="curator-sub",
    preferred_username="carol-curator",
    org="USAREUR-AF",
    rag_roles=["rag-curate:USAREUR-AF", "rag-clearance:SECRET", "rag-releasability:NONE"],
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for value, rank in [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3), ("TOP SECRET", 4)]:
            session.add(ClassificationLevel(value=value, rank=rank))
        session.commit()
        yield session
    engine.dispose()


def _document(**overrides: Any) -> Document:
    fields: dict[str, Any] = {
        "filename": "report.pdf",
        "uploader_sub": "uploader-sub",
        "uploader_username": "alice-ingest",
        "owner_org": "USAREUR-AF",
        "classification": "CUI",
        "releasability": ["NONE"],
        "access_scope": ["ALL_AUTHENTICATED"],
        "source_originator": "USAREUR-AF",
        "doc_type": "report",
        "status": "pending_review",
    }
    fields.update(overrides)
    return Document(**fields)


class _PayloadCalls:
    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        pass

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        pass


@pytest.fixture(autouse=True)
def _stub_qdrant(monkeypatch: pytest.MonkeyPatch) -> _PayloadCalls:
    calls = _PayloadCalls()
    monkeypatch.setattr(curate, "get_store", lambda: calls)
    return calls


def _flagged_advisory(detected_classification: str = "SECRET") -> dict:
    return {
        "assigned_classification": "CUI",
        "detected_classification": detected_classification,
        "under_classified": True,
        "detected_caveats": [],
        "unassigned_caveats": [],
        "evidence": [f"{detected_classification}//NOFORN"],
        "evidence_offsets": [0],
        "notes": [],
        "markings_not_scanned": False,
        "unscanned_reasons": [],
    }


def _precedent_flagged_advisory(top_classification: str = "SECRET") -> dict:
    return {
        "assigned_classification": "CUI",
        "detected_classification": None,
        "under_classified": False,
        "detected_caveats": [],
        "unassigned_caveats": [],
        "evidence": [],
        "evidence_offsets": [],
        "notes": [],
        "markings_not_scanned": False,
        "unscanned_reasons": [],
        "precedent": {
            "similar_count": 5,
            "top_classification": top_classification,
            "top_classification_count": 4,
            "top_releasability": ["NATO"],
            "disagrees_with_assigned": True,
        },
    }


def _llm_flagged_advisory(suggested_classification: str = "SECRET") -> dict:
    return {
        "assigned_classification": "CUI",
        "detected_classification": None,
        "under_classified": False,
        "detected_caveats": [],
        "unassigned_caveats": [],
        "evidence": [],
        "evidence_offsets": [],
        "notes": [],
        "markings_not_scanned": False,
        "unscanned_reasons": [],
        "llm_suggestion": {
            "suggested_classification": suggested_classification,
            "suggested_doc_type": "briefing slide",
            "suggested_program_community": None,
            "confidence": 0.87,
            "rationale": "Mentions troop movements and a classified banner.",
            "disagrees_with_assigned": True,
        },
    }


def _pii_regex_flagged_advisory() -> dict:
    return {
        "assigned_classification": "CUI",
        "detected_classification": None,
        "under_classified": False,
        "detected_caveats": [],
        "unassigned_caveats": [],
        "evidence": [],
        "evidence_offsets": [],
        "notes": [],
        "markings_not_scanned": False,
        "unscanned_reasons": [],
        "pii_advisory": {
            "findings": [
                {
                    "kind": "ssn",
                    "detail": "US Social Security Number pattern",
                    "context": "...[REDACTED]...",
                    "offset": 12,
                },
                {
                    "kind": "credit_card",
                    "detail": "Credit card number pattern (Luhn-valid)",
                    "context": "...[REDACTED]...",
                    "offset": 40,
                },
            ]
        },
    }


def _pii_regex_verified_advisory(*, all_verified: bool = True) -> dict:
    """Issue #380: same two findings as `_pii_regex_flagged_advisory`, but
    each also carries #378's `llm_verdict` -- both saying likely false
    positive. When `all_verified` is False, only the first finding got a
    verdict (simulates #378's verification pass being unavailable partway
    through, or having only ever covered a subset of findings)."""
    advisory = _pii_regex_flagged_advisory()
    findings = advisory["pii_advisory"]["findings"]
    findings[0]["llm_verdict"] = {
        "likely_false_positive": True,
        "rationale": "reads like a part number, not an SSN",
    }
    if all_verified:
        findings[1]["llm_verdict"] = {
            "likely_false_positive": True,
            "rationale": "reads like a catalog reference, not a card number",
        }
    return advisory


def _pii_llm_flagged_advisory() -> dict:
    return {
        "assigned_classification": "CUI",
        "detected_classification": None,
        "under_classified": False,
        "detected_caveats": [],
        "unassigned_caveats": [],
        "evidence": [],
        "evidence_offsets": [],
        "notes": [],
        "markings_not_scanned": False,
        "unscanned_reasons": [],
        "pii_advisory": {
            "llm_findings": [
                {"kind": "spelled-out SSN", "rationale": "Text spells out a nine-digit ID."},
            ]
        },
    }


def _decision_entry(session: Session, action: str) -> AuditLogEntry:
    entries = session.exec(select(AuditLogEntry).where(AuditLogEntry.action == action)).all()
    assert len(entries) == 1
    return entries[0]


class TestApproveLinksFlaggedAdvisory:
    def test_approve_embeds_the_flagged_advisory(self, session: Session) -> None:
        doc = _document(tagging_advisory=_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["flagged_classification"] == "SECRET"
        # No correction was applied -- final tags are still what was flagged.
        assert outcome["final_classification"] == "CUI"
        # Issue #309 (Phase 4): the pre-decision assigned tag and an explicit
        # per-suggester flag, so an offline reader can rank-compare without a
        # second query and can tell this flag apart from a mere marking
        # *detection* that wasn't actually under-classified.
        assert outcome["assigned_classification"] == "CUI"
        assert outcome["marking_mismatch_flagged"] is True

    def test_approve_records_the_correction_when_curator_agrees(self, session: Session) -> None:
        doc = _document(tagging_advisory=_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(
            doc.id,
            corrections=curate.Corrections(classification="SECRET"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["flagged_classification"] == "SECRET"
        assert outcome["final_classification"] == "SECRET"

    def test_approve_of_unflagged_document_has_no_tagging_advisory_link(
        self, session: Session
    ) -> None:
        doc = _document()  # tagging_advisory left unset -- nothing was flagged
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        assert entry.detail["tagging_advisory"] is None

    def test_approve_embeds_a_flagged_precedent_advisory(self, session: Session) -> None:
        # Issue #307 Phase 2: a precedent disagreement links into the
        # decision's audit entry the same way a Phase 1 marking-mismatch
        # finding does, even with no marking-mismatch finding of its own.
        doc = _document(tagging_advisory=_precedent_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["precedent_classification"] == "SECRET"
        assert outcome["precedent_similar_count"] == 5
        # Precedent flagged on its own -- Phase 1's own boolean stays False,
        # distinguishing it from a marking-mismatch flag (issue #309).
        assert outcome["marking_mismatch_flagged"] is False

    def test_approve_of_precedent_agreeing_document_has_no_tagging_advisory_link(
        self, session: Session
    ) -> None:
        advisory = _precedent_flagged_advisory()
        advisory["precedent"]["disagrees_with_assigned"] = False
        doc = _document(tagging_advisory=advisory)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        assert entry.detail["tagging_advisory"] is None

    def test_approve_embeds_a_flagged_llm_suggestion(self, session: Session) -> None:
        # Issue #308 Phase 3: an LLM classification-suggestion disagreement
        # links into the decision's audit entry the same way Phase 1/2's
        # findings do, even with no marking-mismatch or precedent finding of
        # its own.
        doc = _document(tagging_advisory=_llm_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["llm_suggested_classification"] == "SECRET"
        assert outcome["llm_suggested_doc_type"] == "briefing slide"
        assert outcome["llm_confidence"] == 0.87

    def test_approve_of_llm_agreeing_document_has_no_tagging_advisory_link(
        self, session: Session
    ) -> None:
        advisory = _llm_flagged_advisory()
        advisory["llm_suggestion"]["disagrees_with_assigned"] = False
        doc = _document(tagging_advisory=advisory)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        assert entry.detail["tagging_advisory"] is None


class TestPiiAdvisoryLinkage:
    """Issue #345: a sensitive-data-pattern finding (#342 regex / #343
    LLM-assisted) links into the decision's audit entry the same way the
    classification-tag suggesters above do, even with no marking-mismatch,
    precedent, or LLM classification finding of its own."""

    def test_approve_embeds_a_flagged_pii_regex_advisory(self, session: Session) -> None:
        doc = _document(tagging_advisory=_pii_regex_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["pii_regex_kinds"] == ["credit_card", "ssn"]
        assert outcome["pii_regex_count"] == 2
        assert "pii_llm_kinds" not in outcome
        # Issue #380: #378's LLM verification never ran for this fixture (no
        # finding carries an `llm_verdict`), so these keys must be omitted
        # entirely rather than zeroed -- see _tagging_advisory_outcome's
        # docstring on why that distinction matters to the calibration script.
        assert "pii_regex_llm_verified_count" not in outcome
        assert "pii_regex_llm_likely_false_positive_count" not in outcome
        # No marking-mismatch/precedent/LLM finding of its own.
        assert outcome["marking_mismatch_flagged"] is False

    def test_approve_embeds_a_flagged_pii_llm_advisory(self, session: Session) -> None:
        doc = _document(tagging_advisory=_pii_llm_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["pii_llm_kinds"] == ["spelled-out SSN"]
        assert outcome["pii_llm_count"] == 1
        assert "pii_regex_kinds" not in outcome

    def test_reject_embeds_a_flagged_pii_regex_advisory(self, session: Session) -> None:
        doc = _document(tagging_advisory=_pii_regex_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.reject(
            doc.id,
            curate.Rejection(reason="spillage risk per PII advisory"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        entry = _decision_entry(session, "document.reject")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["pii_regex_kinds"] == ["credit_card", "ssn"]

    def test_approve_embeds_llm_verified_counts_when_all_findings_verified(
        self, session: Session
    ) -> None:
        doc = _document(tagging_advisory=_pii_regex_verified_advisory(all_verified=True))
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["pii_regex_count"] == 2
        assert outcome["pii_regex_llm_verified_count"] == 2
        assert outcome["pii_regex_llm_likely_false_positive_count"] == 2
        # Never the verdict's own rationale text -- kinds/counts only, same
        # posture as pii_regex_kinds/content_advisory_kinds.
        assert "rationale" not in str(outcome)

    def test_approve_embeds_partial_verified_count_when_only_some_findings_verified(
        self, session: Session
    ) -> None:
        doc = _document(tagging_advisory=_pii_regex_verified_advisory(all_verified=False))
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["pii_regex_count"] == 2
        # Only the first finding carries an llm_verdict.
        assert outcome["pii_regex_llm_verified_count"] == 1
        assert outcome["pii_regex_llm_likely_false_positive_count"] == 1

    def test_approve_of_no_pii_finding_document_has_no_pii_keys(self, session: Session) -> None:
        advisory = _pii_regex_flagged_advisory()
        advisory["pii_advisory"] = {"findings": []}
        doc = _document(tagging_advisory=advisory)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        entry = _decision_entry(session, "document.approve")
        # Nothing else flagged either, so no tagging_advisory link at all.
        assert entry.detail["tagging_advisory"] is None


class TestRejectLinksFlaggedAdvisory:
    def test_reject_embeds_the_flagged_advisory(self, session: Session) -> None:
        doc = _document(tagging_advisory=_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.reject(
            doc.id,
            curate.Rejection(reason="spillage risk per marking advisory"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        entry = _decision_entry(session, "document.reject")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["flagged_classification"] == "SECRET"

    def test_reject_of_unflagged_document_has_no_tagging_advisory_link(
        self, session: Session
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.reject(
            doc.id,
            curate.Rejection(reason="not relevant"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        entry = _decision_entry(session, "document.reject")
        assert entry.detail["tagging_advisory"] is None

    def test_reject_embeds_a_flagged_llm_suggestion(self, session: Session) -> None:
        doc = _document(tagging_advisory=_llm_flagged_advisory())
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.reject(
            doc.id,
            curate.Rejection(reason="spillage risk per LLM suggestion"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        entry = _decision_entry(session, "document.reject")
        outcome = entry.detail["tagging_advisory"]
        assert outcome["llm_suggested_classification"] == "SECRET"
