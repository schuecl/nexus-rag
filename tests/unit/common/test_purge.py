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
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from common import purge as purge_mod
from common.models import AuditLogEntry, Document
from common.purge import PURGED_STATUS, PURGING_STATUS, SCRUBBED, PurgeError, purge_document


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
