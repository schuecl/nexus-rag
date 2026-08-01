"""Issue #326: _validate_supersede (called from approve() whenever the
document being approved has supersedes_document_id set) used to check the
old document's status before the caller's authority over that old document,
so a curator with authority over the *new* document but not the *old* one
(lower clearance than the original uploader, whose clearance is what
submission-time validation in app/routes/upload.py checked) could learn the
old document's exact non-approved status via the 409 message, before their
lack of authority over it was ever established.

Unlike #322, org can't diverge here -- validate_supersede_target (#325)
already guarantees old_doc.owner_org == new_doc.owner_org at submission
time. The oracle is narrower: same-org, cross-clearance. These tests pin
that a curator lacking authority over old_doc (via clearance, the only leg
that can actually differ) gets the 403 from _check_curator_authority, never
the 409 that would have named old_doc's status.
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


class TestApproveDoesNotLeakOldDocStatusAcrossClearanceBoundary:
    @pytest.mark.parametrize("old_status", ["rejected", "superseded", "failed"])
    def test_no_authority_over_old_doc_gets_403_not_409(
        self, session: Session, old_status: str, _stub_qdrant: _PayloadCalls
    ) -> None:
        # old_doc is above CURATOR's SECRET clearance -- authority must fail
        # on clearance before its non-approved status is ever revealed.
        old_doc = _document(classification="TOP SECRET", status=old_status)
        session.add(old_doc)
        session.commit()
        session.refresh(old_doc)

        new_doc = _document(supersedes_document_id=old_doc.id)
        session.add(new_doc)
        session.commit()
        session.refresh(new_doc)

        with pytest.raises(Exception) as exc_info:
            curate.approve(new_doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]
        # Nothing about the new document's approval or old document's delete
        # made it to the store either -- validation must fail before any
        # mutation (FR-7).
        assert _stub_qdrant.calls == []

    def test_no_authority_over_old_doc_still_403s_when_old_doc_is_approved(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        # Authority is checked regardless of old_doc's status -- even the
        # "would have passed the status check anyway" case must still 403.
        old_doc = _document(classification="TOP SECRET", status="approved")
        session.add(old_doc)
        session.commit()
        session.refresh(old_doc)

        new_doc = _document(supersedes_document_id=old_doc.id)
        session.add(new_doc)
        session.commit()
        session.refresh(new_doc)

        with pytest.raises(Exception) as exc_info:
            curate.approve(new_doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]
        assert _stub_qdrant.calls == []

    def test_authority_over_old_doc_still_gets_the_real_409(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        # The fix must not turn a legitimate in-authority conflict into a 403.
        old_doc = _document(classification="CUI", status="rejected")
        session.add(old_doc)
        session.commit()
        session.refresh(old_doc)

        new_doc = _document(supersedes_document_id=old_doc.id)
        session.add(new_doc)
        session.commit()
        session.refresh(new_doc)

        with pytest.raises(Exception) as exc_info:
            curate.approve(new_doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert exc_info.value.status_code == 409  # type: ignore[attr-defined]
        assert "now 'rejected'" in exc_info.value.detail  # type: ignore[attr-defined]
        assert _stub_qdrant.calls == []
