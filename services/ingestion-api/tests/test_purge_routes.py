"""Issue #279 (gap G3): the two-person purge-request/confirm routes, plus the
PURGE_TWO_PERSON_REQUIRED flag that decides whether the original single-call
DELETE /documents/{id} (issue #123) still works.

Same technique as test_curate_documents.py: call the route functions
directly against an in-memory SQLite session, bypassing the FastAPI layer
(auth/CSRF are Depends() and covered elsewhere), so these tests exercise the
actual authority/flag/two-person logic.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from app.routes import upload
from common import purge as purge_mod
from common.claims import UserClaims
from common.models import Document, PurgeRequest

DAVE = UserClaims(sub="dave-sub", preferred_username="dave-admin", rag_roles=["rag-purge"])
EVE = UserClaims(sub="eve-sub", preferred_username="eve-purge", rag_roles=["rag-purge"])


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _document(**overrides: Any) -> Document:
    fields: dict[str, Any] = {
        "filename": "spilled-secret.pdf",
        "uploader_sub": "alice-sub",
        "uploader_username": "alice-ingest",
        "owner_org": "USAREUR-AF",
        "classification": "CUI",
        "releasability": ["FVEY"],
        "access_scope": ["USAREUR-AF"],
        "source_originator": "USAREUR-AF G2",
        "doc_type": "report",
        "original_object_key": "documents/x/original",
        "status": "approved",
        "chunk_count": 7,
    }
    fields.update(overrides)
    return Document(**fields)


class _FakeStore:
    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        del document_id, classification


class _FakeObjectStore:
    def delete(self, key: str) -> None:
        del key


@pytest.fixture(autouse=True)
def _stub_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(purge_mod, "get_store", _FakeStore)
    monkeypatch.setattr(purge_mod, "get_object_store", _FakeObjectStore)


class TestDeleteRouteFlag:
    def test_single_call_purge_works_when_flag_unset(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(upload, "PURGE_TWO_PERSON_REQUIRED", False)
        doc = _document()
        session.add(doc)
        session.commit()

        result = upload.purge(
            doc.id,
            upload.PurgeReason(reason="spillage"),
            user=DAVE,
            session=session,
            _csrf=None,
        )

        assert result.status == "purged"

    def test_single_call_purge_refused_when_flag_set(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(upload, "PURGE_TWO_PERSON_REQUIRED", True)
        doc = _document()
        session.add(doc)
        session.commit()

        with pytest.raises(HTTPException) as exc_info:
            upload.purge(
                doc.id,
                upload.PurgeReason(reason="spillage"),
                user=DAVE,
                session=session,
                _csrf=None,
            )

        assert exc_info.value.status_code == 409
        assert session.get(Document, doc.id).status == "approved"


class TestCreatePurgeRequest:
    def test_records_a_request_and_destroys_nothing(self, session: Session) -> None:
        doc = _document()
        session.add(doc)
        session.commit()

        req = upload.create_purge_request(
            doc.id,
            upload.PurgeReason(reason="spillage"),
            user=DAVE,
            session=session,
            _csrf=None,
        )

        assert req.status == "pending"
        assert req.requested_by_sub == DAVE.sub
        assert session.get(Document, doc.id).status == "approved"

    def test_unknown_document_is_404(self, session: Session) -> None:
        with pytest.raises(HTTPException) as exc_info:
            upload.create_purge_request(
                uuid.uuid4(),
                upload.PurgeReason(reason="spillage"),
                user=DAVE,
                session=session,
                _csrf=None,
            )

        assert exc_info.value.status_code == 404

    def test_second_pending_request_is_409(self, session: Session) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        upload.create_purge_request(
            doc.id, upload.PurgeReason(reason="first"), user=DAVE, session=session, _csrf=None
        )

        with pytest.raises(HTTPException) as exc_info:
            upload.create_purge_request(
                doc.id,
                upload.PurgeReason(reason="second"),
                user=EVE,
                session=session,
                _csrf=None,
            )

        assert exc_info.value.status_code == 409


class TestConfirmPurgeRequest:
    def test_a_different_holder_confirms_and_executes_the_purge(self, session: Session) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        req = upload.create_purge_request(
            doc.id, upload.PurgeReason(reason="spillage"), user=DAVE, session=session, _csrf=None
        )

        result = upload.confirm_purge_request(doc.id, req.id, user=EVE, session=session, _csrf=None)

        assert result.status == "purged"
        assert session.get(PurgeRequest, req.id).status == "confirmed"
        assert session.get(PurgeRequest, req.id).confirmed_by_sub == EVE.sub

    def test_the_requester_cannot_confirm_their_own_request(self, session: Session) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        req = upload.create_purge_request(
            doc.id, upload.PurgeReason(reason="spillage"), user=DAVE, session=session, _csrf=None
        )

        with pytest.raises(HTTPException) as exc_info:
            upload.confirm_purge_request(doc.id, req.id, user=DAVE, session=session, _csrf=None)

        assert exc_info.value.status_code == 409
        assert session.get(Document, doc.id).status == "approved"

    def test_a_request_id_that_does_not_belong_to_the_document_is_404(
        self, session: Session
    ) -> None:
        doc_a = _document(filename="a.pdf")
        doc_b = _document(filename="b.pdf")
        session.add(doc_a)
        session.add(doc_b)
        session.commit()
        req = upload.create_purge_request(
            doc_a.id, upload.PurgeReason(reason="spillage"), user=DAVE, session=session, _csrf=None
        )

        with pytest.raises(HTTPException) as exc_info:
            upload.confirm_purge_request(doc_b.id, req.id, user=EVE, session=session, _csrf=None)

        assert exc_info.value.status_code == 404

    def test_unknown_request_is_404(self, session: Session) -> None:
        doc = _document()
        session.add(doc)
        session.commit()

        with pytest.raises(HTTPException) as exc_info:
            upload.confirm_purge_request(
                doc.id, uuid.uuid4(), user=EVE, session=session, _csrf=None
            )

        assert exc_info.value.status_code == 404
