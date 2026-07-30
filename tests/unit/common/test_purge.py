"""Coverage for issue #123: a document's content can be destroyed everywhere.

The remediation path for classification spillage. Before this, a mis-tagged
document could be made *unretrievable* -- flip its status and the FR-26 filter
stops matching it -- but never destroyed: the original stayed in the object
store and the chunks stayed in Qdrant with their text in cleartext.

These tests care about two things the issue calls out specifically: that every
store is actually cleared, and that a partial failure always leaves the
document less exposed rather than more.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from common import purge as purge_mod
from common.models import AuditLogEntry, Document, PurgeRequest
from common.purge import (
    CONFIRMED_STATUS,
    PENDING_STATUS,
    PURGED_STATUS,
    PURGING_STATUS,
    SCRUBBED,
    PurgeError,
    PurgeRequestError,
    confirm_purge,
    purge_confirmation_authorized,
    purge_document,
    request_purge,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    # Disposing is not optional housekeeping: pytest 9 turns the
    # ResourceWarning an un-disposed sqlite3 connection raises at GC time into
    # a test error, and because GC runs whenever it likes, the error lands on
    # whichever unrelated test is executing at the time (#188).
    engine.dispose()


@pytest.fixture
def doc(db):
    d = Document(
        filename="spilled-secret.pdf",
        uploader_sub="alice-sub",
        uploader_username="alice-ingest",
        owner_org="USAREUR-AF",
        classification="CUI",
        releasability=["FVEY"],
        access_scope=["USAREUR-AF"],
        source_originator="USAREUR-AF G2",
        doc_type="report",
        program_community="PROG-X",
        original_object_key="documents/x/original",
        status="approved",
        chunk_count=7,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


class _FakeStore:
    """#160: purge now goes through the vector-store seam; tests fake the
    store rather than the old qdrant helper functions."""

    def __init__(self, on_delete):
        self._on_delete = on_delete

    def delete_document_chunks(self, document_id, classification):
        del classification
        self._on_delete(document_id)


@pytest.fixture
def stores(monkeypatch):
    """Record what each store was asked to destroy."""
    calls = {"qdrant": [], "objects": []}

    class _Store:
        def delete(self, key):
            calls["objects"].append(key)

    monkeypatch.setattr(purge_mod, "get_store", lambda: _FakeStore(calls["qdrant"].append))
    monkeypatch.setattr(purge_mod, "get_object_store", _Store)
    return calls


class TestPurgeClearsEveryStore:
    def test_chunks_and_original_are_both_destroyed(self, db, doc, stores):
        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="spillage")

        assert stores["qdrant"] == [str(doc.id)]
        assert stores["objects"] == ["documents/x/original"]

    def test_row_is_tombstoned_not_deleted(self, db, doc, stores):
        """The id has to survive: prior audit entries reference it, and the
        destruction itself needs to be provable."""
        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="spillage")

        row = db.get(Document, doc.id)
        assert row is not None
        assert row.status == PURGED_STATUS

    def test_every_content_bearing_field_is_scrubbed(self, db, doc, stores):
        """Keeping `filename` on a document purged *for* a filename-bearing
        spill would defeat the purpose."""
        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="spillage")

        row = db.get(Document, doc.id)
        assert "spilled-secret" not in str(row.model_dump())
        assert row.filename == SCRUBBED
        assert row.classification == SCRUBBED
        assert row.source_originator == SCRUBBED
        assert row.releasability == []
        assert row.access_scope == []
        assert row.program_community is None
        assert row.original_object_key is None
        assert row.chunk_count == 0


class TestAuditTrail:
    def test_purge_is_audited_with_who_and_why(self, db, doc, stores):
        purge_document(
            db, doc.id, actor_sub="dave-sub", actor_username="dave", reason="SECRET in CUI"
        )

        entry = db.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.purged")
        ).one()
        assert entry.actor_sub == "dave-sub"
        assert entry.target_id == str(doc.id)
        assert entry.detail["reason"] == "SECRET in CUI"
        assert entry.detail["status_before_purge"] == "approved"
        assert entry.detail["chunks_destroyed"] == 7

    def test_audit_entry_does_not_retain_the_filename(self, db, doc, stores):
        """audit_log is append-only (NFR-2) and outlives the document by
        design, so recording the destroyed document's name there would leave
        exactly the content a spillage purge exists to remove."""
        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="spillage")

        entry = db.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.purged")
        ).one()
        assert "spilled-secret" not in str(entry.detail)


class TestPartialFailure:
    def test_qdrant_failure_leaves_the_document_unretrievable(self, db, doc, monkeypatch):
        """The FR-26 filter requires status == approved. If a store fails, the
        status flip must already have landed, so the document is inert even
        though its bytes survive."""

        def _boom(_c, _id):
            raise RuntimeError("qdrant down")

        monkeypatch.setattr(purge_mod, "get_store", lambda: _FakeStore(_boom))

        with pytest.raises(PurgeError, match="retry"):
            purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="r")

        assert db.get(Document, doc.id).status == PURGING_STATUS

    def test_object_store_failure_leaves_chunks_already_destroyed(self, db, doc, monkeypatch):
        """Ordering matters: the vector store is cleared first because it is
        the copy most widely reachable (#110), so a later failure has already
        removed the worse exposure."""
        cleared = []

        class _Store:
            def delete(self, _key):
                raise RuntimeError("s3 down")

        monkeypatch.setattr(purge_mod, "get_store", lambda: _FakeStore(cleared.append))
        monkeypatch.setattr(purge_mod, "get_object_store", _Store)

        with pytest.raises(PurgeError):
            purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="r")

        assert cleared == [str(doc.id)], "chunks should already be gone"
        assert db.get(Document, doc.id).status == PURGING_STATUS

    def test_a_half_finished_purge_can_be_retried(self, db, doc, monkeypatch, stores):
        """Every step is idempotent, so resuming converges rather than erroring."""
        db.get(Document, doc.id).status = PURGING_STATUS
        db.commit()

        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="retry")

        assert db.get(Document, doc.id).status == PURGED_STATUS

    def test_missing_original_is_not_an_error(self, db, doc, monkeypatch):
        class _Store:
            def delete(self, _key):
                raise FileNotFoundError("already gone")

        monkeypatch.setattr(purge_mod, "get_store", lambda: _FakeStore(lambda _i: None))
        monkeypatch.setattr(purge_mod, "get_object_store", _Store)

        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="r")

        assert db.get(Document, doc.id).status == PURGED_STATUS


class TestIdempotenceAndErrors:
    def test_purging_an_already_purged_document_is_a_no_op(self, db, doc, stores):
        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="first")
        stores["qdrant"].clear()

        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="second")

        assert stores["qdrant"] == [], "must not re-run destruction"
        assert (
            len(
                db.exec(
                    select(AuditLogEntry).where(AuditLogEntry.action == "document.purged")
                ).all()
            )
            == 1
        ), "and must not write a second purge audit entry"

    def test_unknown_document_raises_not_found(self, db, stores):
        with pytest.raises(PurgeError, match="not found"):
            purge_document(db, uuid.uuid4(), actor_sub="d", actor_username="dave", reason="r")

    @pytest.mark.parametrize("status", ["queued", "pending_review", "rejected", "approved"])
    def test_any_status_can_be_purged(self, db, stores, status):
        """A spill can be discovered at any point in the lifecycle, and
        rejected/pending documents keep their original file too."""
        d = Document(
            filename="f.pdf",
            uploader_sub="a",
            uploader_username="a",
            owner_org="o",
            classification="CUI",
            releasability=["FVEY"],
            access_scope=["o"],
            source_originator="s",
            doc_type="report",
            original_object_key="k",
            status=status,
            created_at=datetime.now(UTC),
        )
        db.add(d)
        db.commit()

        purge_document(db, d.id, actor_sub="d", actor_username="dave", reason="r")

        assert db.get(Document, d.id).status == PURGED_STATUS


class TestPurgeConfirmationAuthorized:
    """Issue #279 (gap G3): the two-person invariant itself, pulled out as
    its own predicate so it is also pinned by a BDD scenario
    (tests/e2e/features/access_control.feature)."""

    def test_same_sub_is_not_authorized(self):
        assert not purge_confirmation_authorized(
            requested_by_sub="dave-sub", confirming_sub="dave-sub"
        )

    def test_different_sub_is_authorized(self):
        assert purge_confirmation_authorized(requested_by_sub="dave-sub", confirming_sub="eve-sub")


class TestRequestPurge:
    def test_creates_a_pending_request_and_destroys_nothing(self, db, doc, stores):
        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )

        assert req.status == PENDING_STATUS
        assert req.document_id == doc.id
        assert req.requested_by_sub == "dave-sub"
        assert stores["qdrant"] == []
        assert stores["objects"] == []
        assert db.get(Document, doc.id).status == "approved"

    def test_is_audited_as_a_request_not_a_purge(self, db, doc, stores):
        request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )

        entries = db.exec(select(AuditLogEntry)).all()
        assert [e.action for e in entries] == ["document.purge_requested"]
        assert entries[0].detail["reason"] == "spillage"

    def test_unknown_document_raises_not_found(self, db):
        with pytest.raises(PurgeRequestError, match="not found"):
            request_purge(
                db, uuid.uuid4(), actor_sub="d", actor_username="d", reason="r", expiry_hours=24
            )

    def test_already_purged_document_cannot_be_requested(self, db, doc, stores):
        purge_document(db, doc.id, actor_sub="d", actor_username="dave", reason="first")

        with pytest.raises(PurgeRequestError, match="already purged"):
            request_purge(
                db, doc.id, actor_sub="d", actor_username="d", reason="r", expiry_hours=24
            )

    def test_second_unexpired_pending_request_is_refused(self, db, doc):
        request_purge(
            db, doc.id, actor_sub="dave-sub", actor_username="dave", reason="first", expiry_hours=24
        )

        with pytest.raises(PurgeRequestError, match="already has an unexpired pending"):
            request_purge(
                db,
                doc.id,
                actor_sub="eve-sub",
                actor_username="eve",
                reason="second",
                expiry_hours=24,
            )

    def test_a_new_request_is_allowed_once_the_prior_one_expired(self, db, doc):
        stale = request_purge(
            db, doc.id, actor_sub="dave-sub", actor_username="dave", reason="first", expiry_hours=24
        )
        stale.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db.add(stale)
        db.commit()

        fresh = request_purge(
            db, doc.id, actor_sub="eve-sub", actor_username="eve", reason="second", expiry_hours=24
        )

        assert fresh.id != stale.id


class TestConfirmPurge:
    def test_confirming_holder_executes_the_purge(self, db, doc, stores):
        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )

        result = confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")

        assert result.status == PURGED_STATUS
        assert stores["qdrant"] == [str(doc.id)]
        assert db.get(PurgeRequest, req.id).status == CONFIRMED_STATUS
        assert db.get(PurgeRequest, req.id).confirmed_by_sub == "eve-sub"

    def test_both_identities_land_in_the_purge_audit_row(self, db, doc, stores):
        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )

        confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")

        entry = db.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.purged")
        ).one()
        assert entry.actor_sub == "eve-sub"
        assert entry.detail["requested_by_sub"] == "dave-sub"
        assert entry.detail["requested_by_username"] == "dave"

    def test_same_person_confirmation_is_refused(self, db, doc, stores):
        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )

        with pytest.raises(PurgeRequestError, match="different rag-purge holder"):
            confirm_purge(db, req.id, actor_sub="dave-sub", actor_username="dave")

        assert stores["qdrant"] == [], "nothing should have been destroyed"
        assert db.get(Document, doc.id).status == "approved"
        assert db.get(PurgeRequest, req.id).status == PENDING_STATUS

    def test_expired_request_cannot_be_confirmed(self, db, doc, stores):
        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )
        req.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.add(req)
        db.commit()

        with pytest.raises(PurgeRequestError, match="expired"):
            confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")

        assert stores["qdrant"] == []

    def test_unknown_request_raises_not_found(self, db):
        with pytest.raises(PurgeRequestError, match="not found"):
            confirm_purge(db, uuid.uuid4(), actor_sub="eve-sub", actor_username="eve")

    def test_confirming_an_already_confirmed_request_is_idempotent(self, db, doc, stores):
        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )
        confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")
        stores["qdrant"].clear()

        result = confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")

        assert result.status == PURGED_STATUS
        assert stores["qdrant"] == [], "must not re-run destruction"

    def test_a_store_failure_leaves_the_request_pending_and_retryable(self, db, doc, monkeypatch):
        """Same reasoning as purge_document's own partial-failure tests: a
        transient failure must not brick the flow -- confirming again with
        the same (still-authorized) identity should resume it."""
        calls = {"n": 0}

        def _on_delete(_document_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("qdrant down")

        class _NullObjectStore:
            def delete(self, _key):
                pass

        monkeypatch.setattr(purge_mod, "get_store", lambda: _FakeStore(_on_delete))
        monkeypatch.setattr(purge_mod, "get_object_store", _NullObjectStore)

        req = request_purge(
            db,
            doc.id,
            actor_sub="dave-sub",
            actor_username="dave",
            reason="spillage",
            expiry_hours=24,
        )

        with pytest.raises(PurgeError):
            confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")
        assert db.get(PurgeRequest, req.id).status == PENDING_STATUS

        result = confirm_purge(db, req.id, actor_sub="eve-sub", actor_username="eve")
        assert result.status == PURGED_STATUS
        assert db.get(PurgeRequest, req.id).status == CONFIRMED_STATUS
