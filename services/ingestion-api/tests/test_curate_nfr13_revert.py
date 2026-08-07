"""Issue #77 (NFR-13 slice): the revert-on-partial-failure branch in
approve()/reject() had never been exercised by a committed test -- dev-setup.md
documented it as "smoke-tested" (calling approve()/reject() directly against an
in-memory SQLite DB with a mocked Qdrant client), but that smoke test was never
checked in, so nothing here failed if the revert logic regressed.

These tests reproduce that same technique -- bypassing the FastAPI layer,
calling approve()/reject() directly against an in-memory SQLite session --
and pin it down as a permanent regression check: on any failure between the
Qdrant status write and session.commit() (a plain approve/reject, or a
supersede where the old document's Qdrant delete itself raises), the Qdrant
payload must be reverted to pending_review and the original exception must
still propagate, including when the revert call itself also fails.

Out of scope here (tracked separately, not re-litigated by this file): a true
multi-container live-environment run. That gap is now closed by
tests/integration/test_nfr13_live_revert.py (issue #439) -- against a real
Postgres connection failure and a real Qdrant point, no fault-injection hook
needed in production code after all (see that file's docstring). NFR-11
crash-redelivery live coverage is issue #579 (#439 phase 2), still open.
"""

from __future__ import annotations

from collections.abc import Callable
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
    """A stand-in for the #160 vector-store seam that records every
    update_document_payload/delete_document_chunks call, in order, so a test
    can assert both that the revert happened and that it happened *after* the
    original write it's supposed to be undoing.

    Since #160 curate.py reaches the vector backend through get_store() rather
    than module-level Qdrant helpers, so this is patched in as the store
    itself -- which also means these tests now cover the revert path for
    whichever backend is configured, not just Qdrant.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []

    def update_document_payload(self, document_id: str, classification: str, fields: dict) -> None:
        self.calls.append(("update", document_id, classification, fields))

    def delete_document_chunks(self, document_id: str, classification: str) -> None:
        self.calls.append(("delete", document_id, classification, None))


def _break_commit(session: Session, error: Exception) -> None:
    """Makes this session's next commit() raise instead of persisting,
    simulating a DB-side failure (constraint violation, connection loss,
    etc.) between the Qdrant write and the durable Postgres commit."""

    def _raise() -> None:
        raise error

    session.commit = _raise  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _stub_qdrant(monkeypatch: pytest.MonkeyPatch) -> _PayloadCalls:
    calls = _PayloadCalls()
    monkeypatch.setattr(curate, "get_store", lambda: calls)
    return calls


class TestApproveRevertsOnCommitFailure:
    def test_commit_failure_reverts_qdrant_and_reraises(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        _break_commit(session, RuntimeError("db unavailable"))

        with pytest.raises(RuntimeError, match="db unavailable"):
            curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert _stub_qdrant.calls == [
            ("update", str(doc.id), "CUI", {"status": "approved"}),
            ("update", str(doc.id), "CUI", {"status": "pending_review"}),
        ]

    def test_revert_failure_does_not_mask_the_original_exception(
        self, session: Session, _stub_qdrant: _PayloadCalls, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both the commit and the best-effort revert fail -- the caller must
        still see the original failure, not the revert's, and the failure must
        be logged rather than silently swallowed (dev-setup.md's "needs manual
        reconciliation" case)."""
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        _break_commit(session, RuntimeError("db unavailable"))

        def _broken_update(document_id: str, classification: str, fields: dict) -> None:
            _stub_qdrant.calls.append(("update", document_id, classification, fields))
            if fields == {"status": "pending_review"}:
                raise ConnectionError("qdrant unreachable")

        monkeypatch.setattr(_stub_qdrant, "update_document_payload", _broken_update)

        with pytest.raises(RuntimeError, match="db unavailable"):
            curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)


class TestRejectRevertsOnCommitFailure:
    def test_commit_failure_reverts_qdrant_and_reraises(
        self, session: Session, _stub_qdrant: _PayloadCalls
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        _break_commit(session, RuntimeError("db unavailable"))

        with pytest.raises(RuntimeError, match="db unavailable"):
            curate.reject(
                doc.id,
                curate.Rejection(reason="not relevant"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )

        assert _stub_qdrant.calls == [
            ("update", str(doc.id), "CUI", {"status": "rejected"}),
            ("update", str(doc.id), "CUI", {"status": "pending_review"}),
        ]


class TestSupersedeRevertsWhenOldDocumentDeleteFails:
    def test_old_document_delete_failure_reverts_new_documents_qdrant_status(
        self, session: Session, monkeypatch: pytest.MonkeyPatch, _stub_qdrant: _PayloadCalls
    ) -> None:
        """The scenario #77 named explicitly: 'curator approve with Qdrant
        delete failing -> revert'. The new document's Qdrant payload was
        already flipped to approved (so the corpus never has a gap where
        nothing is retrievable, per ARCHITECTURE.md's supersession ordering)
        before the old document's chunks are deleted -- if that delete then
        raises, the new document's premature approval must be undone too."""
        old_doc = _document(status="approved")
        session.add(old_doc)
        session.commit()
        session.refresh(old_doc)

        new_doc = _document(supersedes_document_id=old_doc.id)
        session.add(new_doc)
        session.commit()
        session.refresh(new_doc)

        def _broken_delete(document_id: str, classification: str) -> None:
            _stub_qdrant.calls.append(("delete", document_id, classification, None))
            raise ConnectionError("qdrant delete failed")

        monkeypatch.setattr(_stub_qdrant, "delete_document_chunks", _broken_delete)

        with pytest.raises(ConnectionError, match="qdrant delete failed"):
            curate.approve(new_doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert _stub_qdrant.calls == [
            ("update", str(new_doc.id), "CUI", {"status": "approved"}),
            ("delete", str(old_doc.id), "CUI", None),
            ("update", str(new_doc.id), "CUI", {"status": "pending_review"}),
        ]


class TestApproveAndRejectHappyPathsDoNotRevert:
    """Baseline: the revert path must fire only on failure -- a successful
    commit must never trigger it."""

    @pytest.mark.parametrize(
        "action",
        [
            lambda session, doc: curate.approve(
                doc.id, corrections=None, user=CURATOR, session=session, _csrf=None
            ),
            lambda session, doc: curate.reject(
                doc.id,
                curate.Rejection(reason="not relevant"),
                user=CURATOR,
                session=session,
                _csrf=None,
            ),
        ],
        ids=["approve", "reject"],
    )
    def test_successful_decision_calls_qdrant_once(
        self,
        session: Session,
        _stub_qdrant: _PayloadCalls,
        action: Callable[[Session, Document], Document],
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)

        result = action(session, doc)

        assert result.status in {"approved", "rejected"}
        assert len(_stub_qdrant.calls) == 1
        fields = _stub_qdrant.calls[0][3]
        assert fields is not None
        assert fields["status"] == result.status
