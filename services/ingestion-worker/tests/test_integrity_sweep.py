"""Issue #432: periodic re-verification of object-store originals against
content_sha256, independent of any upload/re-embed event. Covers
app/integrity_sweep.py's batch logic with the DB session and object store
faked, same technique test_reembed.py uses for app/reembed.py."""

from __future__ import annotations

import hashlib
import uuid

from app import integrity_sweep


class _Doc:
    def __init__(self, doc_id, *, content_sha256, original_object_key="documents/x/original"):
        self.id = doc_id
        self.content_sha256 = content_sha256
        self.original_object_key = original_object_key
        self.last_verified_at = None


class _FakeSession:
    def __init__(self, docs_by_id, candidate_ids):
        self._docs = docs_by_id
        self._candidate_ids = candidate_ids
        self.added: list = []
        self.commit_count = 0

    def exec(self, _query):
        return self

    def all(self):
        return self._candidate_ids

    def get(self, _model, doc_id, **_kwargs):
        return self._docs.get(doc_id)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commit_count += 1

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeObjectStore:
    def __init__(self, contents_by_key=None, raise_for_keys=None):
        self._contents = contents_by_key or {}
        self._raise_for = raise_for_keys or set()

    def get(self, key):
        if key in self._raise_for:
            raise FileNotFoundError(key)
        return self._contents[key]


def _wire(monkeypatch, *, docs, candidate_ids, store):
    session = _FakeSession(docs, candidate_ids)
    monkeypatch.setattr(integrity_sweep, "Session", lambda _engine: session)
    monkeypatch.setattr(integrity_sweep, "get_engine", lambda: None)
    monkeypatch.setattr(integrity_sweep, "get_object_store", lambda: store)
    return session


class TestRunSweep:
    def test_verified_document_gets_last_verified_at_stamped(self, monkeypatch):
        doc_id = uuid.uuid4()
        contents = b"hello world"
        digest = hashlib.sha256(contents).hexdigest()
        doc = _Doc(doc_id, content_sha256=digest)
        store = _FakeObjectStore({doc.original_object_key: contents})
        session = _wire(monkeypatch, docs={doc_id: doc}, candidate_ids=[doc_id], store=store)

        report = integrity_sweep.run_sweep(batch_size=10)

        assert report.checked == [str(doc_id)]
        assert report.verified == [str(doc_id)]
        assert report.mismatched == {}
        assert report.missing == {}
        assert report.failures == 0
        assert doc.last_verified_at is not None
        assert doc in session.added
        assert session.commit_count == 1

    def test_digest_mismatch_is_recorded_and_audited_without_status_change(self, monkeypatch):
        doc_id = uuid.uuid4()
        stored_digest = hashlib.sha256(b"original bytes").hexdigest()
        doc = _Doc(doc_id, content_sha256=stored_digest)
        store = _FakeObjectStore({doc.original_object_key: b"tampered bytes"})
        session = _wire(monkeypatch, docs={doc_id: doc}, candidate_ids=[doc_id], store=store)

        report = integrity_sweep.run_sweep(batch_size=10)

        assert list(report.mismatched.keys()) == [str(doc_id)]
        assert report.failures == 1
        # Left unset on a mismatch -- see Document.last_verified_at's comment.
        assert doc.last_verified_at is None
        audit_entries = [e for e in session.added if hasattr(e, "action")]
        assert len(audit_entries) == 1
        entry = audit_entries[0]
        assert entry.action == "document.integrity_check_failed"
        assert entry.target_id == str(doc_id)
        assert entry.detail["reason"] == "digest_mismatch"
        # No digest values leak into the audit trail (purge.py's same concern).
        assert stored_digest not in str(entry.detail)

    def test_missing_original_is_recorded_and_audited(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id, content_sha256="deadbeef")
        store = _FakeObjectStore(raise_for_keys={doc.original_object_key})
        session = _wire(monkeypatch, docs={doc_id: doc}, candidate_ids=[doc_id], store=store)

        report = integrity_sweep.run_sweep(batch_size=10)

        assert list(report.missing.keys()) == [str(doc_id)]
        assert report.failures == 1
        audit_entries = [e for e in session.added if hasattr(e, "action")]
        assert audit_entries[0].detail["reason"] == "original_unreadable"

    def test_batch_size_bounds_the_rolling_window(self, monkeypatch):
        # _candidates() itself does the LIMIT via the query; this asserts the
        # fake's candidate list (standing in for the query result) is what
        # drives how many documents get checked in one run.
        doc_ids = [uuid.uuid4() for _ in range(3)]
        digest = hashlib.sha256(b"x").hexdigest()
        docs = {
            d: _Doc(d, content_sha256=digest, original_object_key=f"documents/{d}/original")
            for d in doc_ids
        }
        store = _FakeObjectStore({doc.original_object_key: b"x" for doc in docs.values()})
        _wire(monkeypatch, docs=docs, candidate_ids=doc_ids[:2], store=store)

        report = integrity_sweep.run_sweep(batch_size=2)

        assert len(report.checked) == 2

    def test_race_between_candidate_selection_and_verification_is_skipped(self, monkeypatch):
        """A document purged (original_object_key cleared) or deleted between
        _candidates() and _verify_one() must not crash or false-flag -- it's
        simply skipped, the next run's window will pick up whatever remains."""
        doc_id = uuid.uuid4()
        store = _FakeObjectStore({})
        session = _wire(monkeypatch, docs={}, candidate_ids=[doc_id], store=store)

        report = integrity_sweep.run_sweep(batch_size=10)

        assert report.checked == []
        assert report.failures == 0
        assert session.added == []


class TestExposition:
    def test_build_exposition_includes_failure_and_heartbeat_metrics(self):
        report = integrity_sweep.SweepReport(
            checked=["a", "b"], verified=["a"], mismatched={"b": "mismatch"}
        )
        text = integrity_sweep.build_exposition(report, 1700000000.0)

        assert "nexus_rag_integrity_check_failures_total 1" in text
        assert "nexus_rag_integrity_check_documents_checked 2" in text
        assert "nexus_rag_integrity_check_last_run_timestamp_seconds 1700000000" in text
