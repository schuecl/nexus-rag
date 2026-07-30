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
    """Same stand-in for the #160 vector-store seam used by
    test_curate_nfr13_revert.py, reused here for the edit endpoint's own
    NFR-13-style revert path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def update_document_payload(self, document_id: str, fields: dict) -> None:
        self.calls.append(("update", document_id, fields))

    def delete_document_chunks(self, document_id: str) -> None:
        self.calls.append(("delete", document_id, None))


def _break_commit(session: Session, error: Exception) -> None:
    def _raise() -> None:
        raise error

    session.commit = _raise  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _stub_qdrant(monkeypatch: pytest.MonkeyPatch) -> _PayloadCalls:
    calls = _PayloadCalls()
    monkeypatch.setattr(curate, "get_store", lambda: calls)
    return calls


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
        assert _stub_qdrant.calls == [("update", str(doc.id), {"classification": "SECRET"})]
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

    def test_403_when_edited_classification_exceeds_curator_clearance(
        self, session: Session
    ) -> None:
        doc = _document(classification="CUI")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.edit_metadata(
                doc.id,
                curate.DocumentEdit(classification="TOP SECRET"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]

    def test_403_when_edited_releasability_exceeds_curator_authority(
        self, session: Session
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.edit_metadata(
                doc.id,
                curate.DocumentEdit(releasability=["NOFORN"]),
                user=CURATOR,
                session=session,
                _csrf=None,
            )
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]

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
            ("update", str(doc.id), {"classification": "SECRET"}),
            ("update", str(doc.id), {"classification": "CUI"}),
        ]
