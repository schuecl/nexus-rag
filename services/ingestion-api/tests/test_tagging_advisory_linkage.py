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
