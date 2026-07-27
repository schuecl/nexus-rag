"""Issue #79 (mypy pass): common/purge.py's purge_document has no status
guard, so a document can be purged while still `queued`/`processing` --
clearing original_object_key as part of that (see purge.py's tombstone step).

Regression coverage for the race mypy's arg-type check on ObjectStore.get()
surfaced: without an explicit None check, FilesystemObjectStore.get(None)
raises a bare TypeError (Path(None) has no __fspath__), which fell through to
process_document's generic `except Exception` branch -- treated as
transient and left unacked, so JetStream would redeliver a message that can
never succeed until the redelivery budget was exhausted, instead of landing
in `failed` immediately like the sibling FileNotFoundError case a few lines
below.
"""

from __future__ import annotations

import uuid

from app import processing


class _Doc:
    def __init__(self):
        self.id = uuid.uuid4()
        self.filename = "report.pdf"
        self.uploader_sub = "alice-sub"
        self.uploader_username = "alice-ingest"
        self.original_object_key = None
        self.status = "processing"
        self.chunk_count = 0
        self.processing_error = None


class _Session:
    def __init__(self, doc):
        self._doc = doc

    def get(self, _model, _id):
        return self._doc

    def add(self, _obj):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


async def test_purged_original_key_fails_permanently_instead_of_retrying(monkeypatch):
    doc = _Doc()
    monkeypatch.setattr(processing, "Session", lambda _engine: _Session(doc))
    monkeypatch.setattr(processing, "get_engine", object)
    monkeypatch.setattr(processing, "AuditLogEntry", lambda **_kw: object())

    terminal = await processing.process_document(doc.id)

    assert terminal is True, "a purged original must be acked, not redelivered forever"
    assert doc.status == "failed"
    assert "purged" in doc.processing_error
