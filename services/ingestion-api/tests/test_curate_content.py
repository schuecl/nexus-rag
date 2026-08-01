"""Issue #284: GET /curate/{doc_id}/content -- the curator content view.
Before this route, no route anywhere in ingestion-api served a curator the
parsed text of a document they were being asked to approve (the curation
gate reviewed metadata, not content). Same technique as
test_curate_documents.py: call the route function directly against an
in-memory SQLite session, bypassing the FastAPI layer.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.routes import curate
from common.claims import UserClaims
from common.models import ClassificationLevel, Document

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
        "status": "pending_review",
    }
    fields.update(overrides)
    return Document(**fields)


class _FakeStore:
    """Stand-in for the #160 vector-store seam. Records which (document_id,
    classification) fetch_document_chunks was called with, so a test can
    confirm the route reads chunks from the document's *current* collection,
    not the response of a different document."""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, str]] = []

    def fetch_document_chunks(self, document_id: str, classification: str) -> list[dict]:
        self.calls.append((document_id, classification))
        return self.chunks


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore(
        chunks=[
            {"chunk_index": 1, "text": "second chunk", "heading": "II", "content_type": "text"},
            {"chunk_index": 0, "text": "first chunk", "heading": "I", "content_type": "text"},
        ]
    )
    monkeypatch.setattr(curate, "get_store", lambda: store)
    return store


class TestGetContent:
    def test_curator_sees_chunk_text(self, session: Session, fake_store: _FakeStore) -> None:
        # Ordering is the vector store's contract (qdrant_store.fetch_document_
        # chunks sorts by chunk_index) -- this route trusts whatever order the
        # store returns, so it's covered separately, not re-asserted here.
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.get_content(doc.id, user=CURATOR, session=session)

        assert {c.chunk_index for c in result} == {0, 1}
        assert {c.text for c in result} == {"first chunk", "second chunk"}
        assert fake_store.calls == [(str(doc.id), "CUI")]

    def test_no_chunks_yet_returns_empty_list(
        self, session: Session, fake_store: _FakeStore
    ) -> None:
        fake_store.chunks = []
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        assert curate.get_content(doc.id, user=CURATOR, session=session) == []

    def test_404_for_missing_document(self, session: Session, fake_store: _FakeStore) -> None:
        with pytest.raises(Exception) as exc_info:
            curate.get_content(curate.uuid.uuid4(), user=CURATOR, session=session)
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]

    def test_404_for_document_outside_curatable_orgs(
        self, session: Session, fake_store: _FakeStore
    ) -> None:
        # Issue #215/#322: existence-oracle semantics -- an org the caller
        # cannot curate at all must 404, indistinguishable from "no such
        # document", the same as approve()/reject() already give.
        doc = _document(owner_org="Signal-Corps")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.get_content(doc.id, user=CURATOR, session=session)
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]
        assert fake_store.calls == []

    def test_403_above_curator_clearance(self, session: Session, fake_store: _FakeStore) -> None:
        doc = _document(classification="TOP SECRET")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.get_content(doc.id, user=CURATOR, session=session)
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]
        assert fake_store.calls == []

    def test_403_unheld_releasability(self, session: Session, fake_store: _FakeStore) -> None:
        doc = _document(releasability=["NOFORN"])
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.get_content(doc.id, user=CURATOR, session=session)
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]

    def test_403_outside_access_scope(self, session: Session, fake_store: _FakeStore) -> None:
        # Issue #277 (gap G1): access_scope is enforced here exactly the same
        # way it already gates approve()/reject() for a pending document.
        doc = _document(access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.get_content(doc.id, user=CURATOR, session=session)
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]

    def test_in_scope_curator_can_read_it(self, session: Session, fake_store: _FakeStore) -> None:
        doc = _document(access_scope=["Signal-Corps"])
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.get_content(doc.id, user=SIGNAL_CORPS_CURATOR, session=session)

        assert len(result) == 2

    def test_other_org_curator_gets_404_not_a_status_leak(
        self, session: Session, fake_store: _FakeStore
    ) -> None:
        doc = _document(owner_org="Signal-Corps", status="approved")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.get_content(doc.id, user=CURATOR, session=session)
        # Not 409 "document is already approved" -- that would leak the
        # document's existence and status to a curator with no authority
        # over its org at all (issue #322).
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]

    @pytest.mark.parametrize("status", ["approved", "rejected", "superseded", "queued"])
    def test_409_once_no_longer_pending_review(
        self, session: Session, fake_store: _FakeStore, status: str
    ) -> None:
        doc = _document(status=status)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.get_content(doc.id, user=CURATOR, session=session)
        assert exc_info.value.status_code == 409  # type: ignore[attr-defined]
        assert fake_store.calls == []

    def test_extra_payload_fields_are_dropped(
        self, session: Session, fake_store: _FakeStore
    ) -> None:
        # ChunkContent is a narrow, explicit schema -- fields that exist in
        # the raw Qdrant payload (document_id, classification, access_scope,
        # status, embedding_model, ...) but aren't part of it must not leak
        # into the response just because a future change adds something new
        # to the stored payload.
        fake_store.chunks = [
            {
                "chunk_index": 0,
                "text": "chunk text",
                "document_id": "leaked",
                "classification": "TOP SECRET",
                "access_scope": ["Signal-Corps"],
                "status": "pending_review",
            }
        ]
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.get_content(doc.id, user=CURATOR, session=session)

        assert result[0].model_dump() == {
            "chunk_index": 0,
            "heading": None,
            "page_or_slide": None,
            "content_type": "text",
            "text": "chunk text",
        }
