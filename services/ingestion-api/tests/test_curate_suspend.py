"""Issue #478: a single curator's reversible way to stop serving an already
`approved` document. reject() 409s once a document has left `pending_review`,
and the two-person purge flow (#279 gap G3) is the right gate for
*destruction* but not for simply taking something out of circulation.

suspend() demotes back to `pending_review` -- the same reversible target
edit_metadata's #268 authority-mismatch demotion already uses -- so these
tests mirror the existing coverage for that demotion path
(test_curate_nfr13_revert.py, test_curate_322_oracle.py) rather than
inventing a new shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.routes import curate
from common.claims import UserClaims
from common.models import ClassificationLevel, Document, Notification

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


def _approved_document(**overrides: Any) -> Document:
    now = datetime.now(UTC)
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
        "reviewed_by_sub": "some-other-curator",
        "reviewed_at": now,
        "first_approved_at": now,
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


def _break_commit(session: Session, error: Exception) -> None:
    def _raise() -> None:
        raise error

    session.commit = _raise  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _stub_qdrant(monkeypatch: pytest.MonkeyPatch) -> _PayloadCalls:
    calls = _PayloadCalls()
    monkeypatch.setattr(curate, "get_store", lambda: calls)
    return calls


class TestSuspendHappyPath:
    def test_approved_document_is_demoted_to_pending_review(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _approved_document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = curate.suspend(
            doc.id,
            curate.Suspension(reason="wrong classification"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.status == "pending_review"
        assert result.reviewed_by_sub is None
        assert result.reviewed_at is None
        assert _stub_qdrant.calls == [
            ("update", str(doc.id), "CUI", {"status": "pending_review"}),
        ]

    def test_first_approved_at_survives_suspension(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        """#286: the chat-plane purge signal needs this to survive the
        demotion, same as edit_metadata's #268 demotion already preserves it."""
        doc = _approved_document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        original_first_approved_at = doc.first_approved_at

        result = curate.suspend(
            doc.id,
            curate.Suspension(reason="spillage review"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        assert result.first_approved_at == original_first_approved_at

    def test_notifies_the_uploader(self, session: Session, _stub_qdrant: _PayloadCalls) -> None:
        doc = _approved_document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.suspend(
            doc.id,
            curate.Suspension(reason="wrong releasability"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        row = session.exec(select(Notification).where(Notification.document_id == doc.id)).first()
        assert row is not None
        assert row.decision == "suspended"
        assert "wrong releasability" in row.message

    def test_suspended_document_reaches_the_pending_review_queue(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _approved_document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        curate.suspend(
            doc.id,
            curate.Suspension(reason="re-review needed"),
            user=CURATOR,
            session=session,
            _csrf=None,
        )

        queued = curate.list_queue(user=CURATOR, session=session)
        assert doc.id in {d.id for d in queued}


class TestSuspendOnlyAppliesToApproved:
    @pytest.mark.parametrize(
        "status", ["pending_review", "rejected", "superseded", "failed", "purging", "purged"]
    )
    def test_non_approved_status_409s(
        self, session: Session, status: str, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _approved_document(status=status)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.suspend(
                doc.id, curate.Suspension(reason="x"), user=CURATOR, session=session, _csrf=None
            )

        assert exc_info.value.status_code == 409  # type: ignore[attr-defined]
        assert _stub_qdrant.calls == []


class TestSuspendDoesNotLeakStatusAcrossOrgBoundary:
    """Same #215/#322 ordering _load_pending already enforces: existence,
    then org authority, then status -- an out-of-org caller must get 404,
    not a 409 that confirms the id exists."""

    @pytest.mark.parametrize("status", ["approved", "rejected", "pending_review"])
    def test_out_of_org_document_404s_regardless_of_status(
        self, session: Session, status: str, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _approved_document(owner_org="Signal-Corps", status=status)
        session.add(doc)
        session.commit()
        session.refresh(doc)

        with pytest.raises(Exception) as exc_info:
            curate.suspend(
                doc.id, curate.Suspension(reason="x"), user=CURATOR, session=session, _csrf=None
            )

        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]
        assert _stub_qdrant.calls == []

    def test_missing_document_also_404s(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        with pytest.raises(Exception) as exc_info:
            curate.suspend(
                curate.uuid.uuid4(),
                curate.Suspension(reason="x"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )
        assert exc_info.value.status_code == 404  # type: ignore[attr-defined]


class TestSuspendRevertsOnCommitFailure:
    def test_commit_failure_reverts_qdrant_and_reraises(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _approved_document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        _break_commit(session, RuntimeError("db unavailable"))

        with pytest.raises(RuntimeError, match="db unavailable"):
            curate.suspend(
                doc.id, curate.Suspension(reason="x"), user=CURATOR, session=session, _csrf=None
            )

        assert _stub_qdrant.calls == [
            ("update", str(doc.id), "CUI", {"status": "pending_review"}),
            ("update", str(doc.id), "CUI", {"status": "approved"}),
        ]

    def test_revert_failure_does_not_mask_the_original_exception(
        self, session: Session, _stub_qdrant: _PayloadCalls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc = _approved_document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        _break_commit(session, RuntimeError("db unavailable"))

        def _broken_update(document_id: str, classification: str, fields: dict) -> None:
            _stub_qdrant.calls.append(("update", document_id, classification, fields))
            if fields == {"status": "approved"}:
                raise ConnectionError("qdrant unreachable")

        monkeypatch.setattr(_stub_qdrant, "update_document_payload", _broken_update)

        with pytest.raises(RuntimeError, match="db unavailable"):
            curate.suspend(
                doc.id, curate.Suspension(reason="x"), user=CURATOR, session=session, _csrf=None
            )
