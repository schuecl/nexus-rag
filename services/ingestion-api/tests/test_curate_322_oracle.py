"""Issue #322: approve()/reject() (via _load_pending) used to check a
document's status before the caller's org authority, so a curator holding
*some* `rag-curate:<org>` role -- just not this document's -- could learn
that an out-of-org document id exists and its exact non-pending status (the
409 message names it) via POST /curate/{id}/approve or /reject, without ever
passing `_check_curator_authority`.

Issue #215 established that an out-of-org caller must get 404,
indistinguishable from "no such document" -- these tests pin that the same
guarantee now holds for the status-conflict path too, the same way
test_curate_content.py already pins it for GET /curate/{id}/content (the
route #284 added with the correct ordering from the start).
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
        "owner_org": "Signal-Corps",  # not in CURATOR's curatable_orgs
        "classification": "CUI",
        "releasability": ["NONE"],
        "access_scope": ["ALL_AUTHENTICATED"],
        "source_originator": "Signal-Corps",
        "doc_type": "report",
        "status": "approved",
    }
    fields.update(overrides)
    return Document(**fields)


class _PayloadCalls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []

    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        self.calls.append(("update", document_id, classification, fields))

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        self.calls.append(("delete", document_id, classification, None))


@pytest.fixture(autouse=True)
def _stub_qdrant(monkeypatch: pytest.MonkeyPatch) -> _PayloadCalls:
    calls = _PayloadCalls()
    monkeypatch.setattr(curate, "get_store", lambda: calls)
    return calls


class TestApproveDoesNotLeakStatusAcrossOrgBoundary:
    @pytest.mark.parametrize(
        "status", ["approved", "rejected", "superseded", "failed", "purging", "purged"]
    )
    def test_out_of_org_document_404s_regardless_of_status(
        self, session: Session, status: str, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document(status=status)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        # Not 409 "document is already <status>" -- that would confirm the
        # id exists and reveal its status to a curator with no authority
        # over its org at all.
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]
        assert _stub_qdrant.calls == []

    def test_out_of_org_pending_document_also_404s(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document(status="pending_review")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]

    def test_missing_document_also_404s(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        with pytest.raises(Exception) as exc_info:
            curate.approve(
                curate.uuid.uuid4(), corrections=None, user=CURATOR, session=session, _csrf=None
            )
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]

    def test_in_org_still_gets_the_real_409(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        # The fix must not turn a legitimate in-org conflict into a 404 too.
        doc = _document(owner_org="USAREUR-AF", status="rejected")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert exc_info.value.status_code == 409  # type: ignore[attr-defined]
        assert "already rejected" in exc_info.value.detail  # type: ignore[attr-defined]


class TestRejectDoesNotLeakStatusAcrossOrgBoundary:
    @pytest.mark.parametrize("status", ["approved", "rejected", "superseded", "failed"])
    def test_out_of_org_document_404s_regardless_of_status(
        self, session: Session, status: str, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document(status=status)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.reject(
                doc.id,
                curate.Rejection(reason="no"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )

        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]
        assert _stub_qdrant.calls == []

    def test_in_org_still_gets_the_real_409(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document(owner_org="USAREUR-AF", status="approved")
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.reject(
                doc.id,
                curate.Rejection(reason="no"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )

        assert exc_info.value.status_code == 409  # type: ignore[attr-defined]
        assert "already approved" in exc_info.value.detail  # type: ignore[attr-defined]
