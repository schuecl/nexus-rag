"""Issue #266: the curation "List" dashboard's backend -- GET /curate/documents
(the master list, any status, scoped identically to /curate/queue) and
PATCH /curate/documents/{id} (post-ingestion metadata correction).

Same technique as test_curate_nfr13_revert.py: call the route functions
directly against an in-memory SQLite session, bypassing the FastAPI layer, so
these tests exercise the actual authority/scoping/NFR-13 logic rather than
HTTP plumbing.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
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

# Issue #277: a same-org curator who additionally holds the Signal-Corps
# group -- CURATOR above deliberately holds neither that group nor sub, so
# it stands in for an org-authorized-but-out-of-scope curator.
SIGNAL_CORPS_CURATOR = UserClaims(
    sub="signal-corps-curator-sub",
    preferred_username="sam-curator",
    org="USAREUR-AF",
    groups=["Signal-Corps"],
    rag_roles=["rag-curate:USAREUR-AF", "rag-clearance:SECRET", "rag-releasability:NONE"],
)

OTHER_ORG_CURATOR = UserClaims(
    sub="other-curator-sub",
    preferred_username="oscar-curator",
    org="Signal-Corps",
    rag_roles=["rag-curate:Signal-Corps", "rag-clearance:SECRET", "rag-releasability:NONE"],
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
        "status": "approved",
    }
    fields.update(overrides)
    return Document(**fields)


class _PayloadCalls:
    """Same stand-in for the #160/#229 vector-store seam used by
    test_curate_nfr13_revert.py, reused here for the edit endpoint's own
    NFR-13-style revert path. `classification` is the collection the chunks
    were stamped with *before* this call (issue #229)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []

    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        self.calls.append(("update", document_id, classification, fields))

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        self.calls.append(("delete", document_id, classification, None))


def _break_commit(session: Session, error: Exception) -> None:
    def _raise() -> None:
        raise error

    session.commit = _raise  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _stub_qdrant(monkeypatch: pytest.MonkeyPatch) -> _PayloadCalls:
    calls = _PayloadCalls()
    monkeypatch.setattr(curate, "get_store", lambda: calls)
    return calls


class TestListQueue:
    """Issue #273: /curate/queue must not surface a pending document the
    curator lacks clearance or releasability authority for, even when it's
    owned by an org they otherwise curate."""

    def test_document_above_curator_clearance_is_hidden(self, session: Session) -> None:
        visible = _document(status="pending_review", classification="CUI")
        above_clearance = _document(
            status="pending_review", classification="TOP SECRET", filename="ts.pdf"
        )
        session.add(visible)
        session.add(above_clearance)
        session.commit()

        docs = curate.list_queue(user=CURATOR, session=session)

        assert [d.id for d in docs] == [visible.id]

    def test_document_with_unheld_releasability_is_hidden(self, session: Session) -> None:
        visible = _document(status="pending_review", releasability=["NONE"])
        noforn = _document(status="pending_review", releasability=["NOFORN"], filename="noforn.pdf")
        session.add(visible)
        session.add(noforn)
        session.commit()

        docs = curate.list_queue(user=CURATOR, session=session)

        assert [d.id for d in docs] == [visible.id]

    def test_still_scoped_to_curatable_orgs(self, session: Session) -> None:
        mine = _document(status="pending_review")
        someone_elses = _document(
            status="pending_review", owner_org="Signal-Corps", filename="x.pdf"
        )
        session.add(mine)
        session.add(someone_elses)
        session.commit()

        docs = curate.list_queue(user=CURATOR, session=session)

        assert [d.id for d in docs] == [mine.id]


class TestScopeGating:
    """Issue #277 (gap G1): access_scope is a hard requirement for seeing a
    *pending* document, on par with clearance/releasability -- no grace
    period, no fallback. A curator outside a document's access_scope simply
    never sees it in the queue; there is no time after which visibility
    opens up to them regardless."""

    def test_out_of_scope_curator_never_sees_it(self, session: Session) -> None:
        doc = _document(status="pending_review", access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()

        docs = curate.list_queue(user=CURATOR, session=session)

        assert docs == []

    def test_in_scope_curator_sees_it(self, session: Session) -> None:
        doc = _document(status="pending_review", access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()

        docs = curate.list_queue(user=SIGNAL_CORPS_CURATOR, session=session)

        assert [d.id for d in docs] == [doc.id]

    def test_all_authenticated_scope_is_never_gated(self, session: Session) -> None:
        doc = _document(status="pending_review", access_scope=["ALL_AUTHENTICATED"])
        session.add(doc)
        session.commit()

        docs = curate.list_queue(user=CURATOR, session=session)

        assert [d.id for d in docs] == [doc.id]

    def test_approved_documents_are_never_scope_gated(self, session: Session) -> None:
        # _visible_to_curator only applies the access_scope check to
        # pending_review rows -- list_documents (the "any status" master
        # list) must not start hiding already-decided documents.
        doc = _document(status="approved", access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()

        docs = curate.list_documents(
            status_filter=None, classification=None, q=None, user=CURATOR, session=session
        )

        assert [d.id for d in docs] == [doc.id]

    def test_out_of_scope_curator_cannot_approve_directly(self, session: Session) -> None:
        # Not just hidden from the queue -- the actual access-control point
        # (approve/reject) has to refuse it too, or hiding it from the list
        # would be security theater against a curator who already has the id.
        doc = _document(status="pending_review", access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()

        with pytest.raises(Exception) as excinfo:
            curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert excinfo.value.status_code == 403  # type: ignore[attr-defined]

    def test_out_of_scope_curator_cannot_reject_directly(self, session: Session) -> None:
        doc = _document(status="pending_review", access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()

        with pytest.raises(Exception) as excinfo:
            curate.reject(
                doc.id, curate.Rejection(reason="no"), user=CURATOR, session=session, _csrf=None
            )

        assert excinfo.value.status_code == 403  # type: ignore[attr-defined]

    def test_in_scope_curator_can_approve(self, session: Session) -> None:
        doc = _document(status="pending_review", access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()

        approved = curate.approve(
            doc.id, corrections=None, user=SIGNAL_CORPS_CURATOR, session=session, _csrf=None
        )

        assert approved.status == "approved"


class TestListDocuments:
    def test_scoped_to_curatable_orgs_across_every_status(self, session: Session) -> None:
        mine_approved = _document(status="approved")
        mine_pending = _document(status="pending_review", filename="draft.pdf")
        someone_elses = _document(status="approved", owner_org="Signal-Corps")
        for doc in (mine_approved, mine_pending, someone_elses):
            session.add(doc)
        session.commit()

        docs = curate.list_documents(
            status_filter=None, classification=None, q=None, user=CURATOR, session=session
        )

        assert {d.id for d in docs} == {mine_approved.id, mine_pending.id}

    def test_status_filter_narrows_the_scoped_set(self, session: Session) -> None:
        approved = _document(status="approved")
        rejected = _document(status="rejected", filename="bad.pdf")
        session.add(approved)
        session.add(rejected)
        session.commit()

        docs = curate.list_documents(
            status_filter="rejected", classification=None, q=None, user=CURATOR, session=session
        )

        assert [d.id for d in docs] == [rejected.id]

    def test_classification_filter(self, session: Session) -> None:
        cui = _document(classification="CUI")
        secret = _document(classification="SECRET", filename="s.pdf")
        session.add(cui)
        session.add(secret)
        session.commit()

        docs = curate.list_documents(
            status_filter=None, classification="SECRET", q=None, user=CURATOR, session=session
        )

        assert [d.id for d in docs] == [secret.id]

    def test_search_matches_filename_originator_type_and_uploader(self, session: Session) -> None:
        target = _document(filename="Quarterly Ops Report.pdf")
        other = _document(filename="unrelated.pdf", source_originator="J2", doc_type="memo")
        session.add(target)
        session.add(other)
        session.commit()

        docs = curate.list_documents(
            status_filter=None, classification=None, q="ops report", user=CURATOR, session=session
        )

        assert [d.id for d in docs] == [target.id]

    def test_document_above_curator_clearance_is_hidden_regardless_of_status(
        self, session: Session
    ) -> None:
        """Issue #273: unlike list_queue, this covers *any* status -- an
        already-approved document above the curator's clearance must not
        appear on the master list either."""
        visible = _document(status="approved", classification="CUI")
        above_clearance = _document(
            status="approved", classification="TOP SECRET", filename="ts.pdf"
        )
        session.add(visible)
        session.add(above_clearance)
        session.commit()

        docs = curate.list_documents(
            status_filter=None, classification=None, q=None, user=CURATOR, session=session
        )

        assert [d.id for d in docs] == [visible.id]

    def test_document_with_unheld_releasability_is_hidden(self, session: Session) -> None:
        visible = _document(status="approved", releasability=["NONE"])
        fvey = _document(status="approved", releasability=["FVEY"], filename="fvey.pdf")
        session.add(visible)
        session.add(fvey)
        session.commit()

        docs = curate.list_documents(
            status_filter=None, classification=None, q=None, user=CURATOR, session=session
        )

        assert [d.id for d in docs] == [visible.id]

    def test_no_curatable_orgs_returns_nothing(self, session: Session) -> None:
        session.add(_document())
        session.commit()

        docs = curate.list_documents(
            status_filter=None,
            classification=None,
            q=None,
            user=OTHER_ORG_CURATOR,
            session=session,
        )

        assert docs == []


class TestDocumentEditRejectsEmptyLists:
    """FR-20/Section 6.3's "one or more" cardinality, same as upload-time
    DocumentMetadataIn -- an edit must not be able to orphan a document by
    clearing its releasability/access_scope down to nothing."""

    def test_empty_releasability_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            curate.DocumentEdit(releasability=[])

    def test_empty_access_scope_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            curate.DocumentEdit(access_scope=[])


class TestEditMetadata:
    def test_edits_qdrant_backed_fields_and_writes_audit_entry(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.edit_metadata(
            doc.id,
            curate.DocumentEdit(classification="SECRET", program_community="J2"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.classification == "SECRET"
        assert result.program_community == "J2"
        assert _stub_qdrant.calls == [("update", str(doc.id), "CUI", {"classification": "SECRET"})]
        entries = session.exec(select(AuditLogEntry)).all()
        assert len(entries) == 1
        assert entries[0].action == "document.metadata_edit"
        assert entries[0].actor_username == "carol-curator"
        assert set(entries[0].detail["fields"]) == {"classification", "program_community"}

    def test_edit_of_non_qdrant_fields_does_not_touch_the_vector_store(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.edit_metadata(
            doc.id,
            curate.DocumentEdit(doc_type="regulation", effective_date="2026-01-01"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.doc_type == "regulation"
        assert result.effective_date == "2026-01-01"
        assert _stub_qdrant.calls == []

    def test_404_for_document_outside_curatable_orgs(self, session: Session) -> None:
        doc = _document(owner_org="Signal-Corps")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.edit_metadata(
                doc.id,
                curate.DocumentEdit(doc_type="x"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "status", ["queued", "processing", "embedded", "superseded", "purging", "purged"]
    )
    def test_409_for_non_editable_statuses(self, session: Session, status: str) -> None:
        doc = _document(status=status)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.edit_metadata(
                doc.id,
                curate.DocumentEdit(doc_type="x"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )
        assert exc_info.value.status_code == 409  # type: ignore[attr-defined]

    def test_editing_unrelated_field_succeeds_even_above_curator_clearance(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        """Issue #268: this used to 403 outright -- edit_metadata re-checked
        the document's *current* classification/releasability against the
        caller's authority before looking at what was actually being edited,
        so any edit of a document tagged above a curator's clearance failed,
        even one that never touched classification/releasability at all."""
        doc = _document(classification="TOP SECRET")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.edit_metadata(
            doc.id,
            curate.DocumentEdit(doc_type="regulation"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.doc_type == "regulation"
        assert result.classification == "TOP SECRET"
        assert result.status == "approved"
        assert _stub_qdrant.calls == []

    def test_classification_beyond_curator_clearance_is_applied_and_demoted(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        """Issue #268: a curator without clearance for the value they're
        setting doesn't get the request rejected outright -- the edit is
        applied, but the document is sent back to pending_review so a curator
        who does hold that authority has to sign off before it's retrievable
        again."""
        doc = _document(classification="CUI")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.edit_metadata(
            doc.id,
            curate.DocumentEdit(classification="TOP SECRET"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.classification == "TOP SECRET"
        assert result.status == "pending_review"
        assert result.reviewed_by_sub is None
        assert result.reviewed_at is None
        assert _stub_qdrant.calls == [
            (
                "update",
                str(doc.id),
                "CUI",
                {"classification": "TOP SECRET", "status": "pending_review"},
            )
        ]
        entries = session.exec(select(AuditLogEntry)).all()
        assert entries[0].detail["demoted_to_pending_review"] is True

    def test_releasability_beyond_curator_authority_is_applied_and_demoted(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.edit_metadata(
            doc.id,
            curate.DocumentEdit(releasability=["NOFORN"]),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.releasability == ["NOFORN"]
        assert result.status == "pending_review"
        assert _stub_qdrant.calls == [
            (
                "update",
                str(doc.id),
                "CUI",
                {"releasability": ["NOFORN"], "status": "pending_review"},
            )
        ]

    def test_edit_within_curator_authority_leaves_status_untouched(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document(classification="CUI", status="rejected")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.edit_metadata(
            doc.id,
            curate.DocumentEdit(classification="SECRET"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.classification == "SECRET"
        assert result.status == "rejected"
        assert _stub_qdrant.calls == [("update", str(doc.id), "CUI", {"classification": "SECRET"})]

    def test_commit_failure_reverts_qdrant_and_reraises(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document(classification="CUI")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        _break_commit(session, RuntimeError("db unavailable"))

        with pytest.raises(RuntimeError, match="db unavailable"):
            curate.edit_metadata(
                doc.id,
                curate.DocumentEdit(classification="SECRET"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )

        assert _stub_qdrant.calls == [
            ("update", str(doc.id), "CUI", {"classification": "SECRET"}),
            ("update", str(doc.id), "SECRET", {"classification": "CUI"}),
        ]
