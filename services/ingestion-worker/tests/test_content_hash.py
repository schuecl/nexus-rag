"""Issue #285: ingestion-worker re-hashes the bytes it fetches from the
object store and refuses to process a mismatch -- the actual integrity
check, at the trust boundary between ingestion-api (services/ingestion-api/
tests/test_upload_content_hash.py covers the digest it computes) and this
service. Same fake-Session technique as test_purge_race.py.
"""

from __future__ import annotations

import hashlib
import uuid

from app import processing
from app.parsing import ParsingError


class _Doc:
    def __init__(self, *, content_sha256: str | None):
        self.id = uuid.uuid4()
        self.filename = "report.txt"
        self.uploader_sub = "alice-sub"
        self.uploader_username = "alice-ingest"
        self.original_object_key = "documents/some-key"
        self.content_sha256 = content_sha256
        self.status = "processing"
        self.processing_started_at = None
        self.chunk_count = 0
        self.processing_error = None
        self.updated_at = None


class _Session:
    def __init__(self, doc):
        self._doc = doc

    def get(self, _model, _id, **_kwargs):
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


class _FakeObjectStore:
    def __init__(self, content: bytes):
        self._content = content

    def get(self, _key: str) -> bytes:
        return self._content


async def test_hash_mismatch_fails_permanently_instead_of_retrying(monkeypatch):
    real_content = b"the actual approved bytes"
    tampered_content = b"different bytes now sitting under the same key"
    doc = _Doc(content_sha256=hashlib.sha256(real_content).hexdigest())

    monkeypatch.setattr(processing, "Session", lambda _engine: _Session(doc))
    monkeypatch.setattr(processing, "get_engine", object)
    monkeypatch.setattr(processing, "AuditLogEntry", lambda **_kw: object())
    monkeypatch.setattr(processing, "get_object_store", lambda: _FakeObjectStore(tampered_content))

    terminal = await processing.process_document(doc.id)

    assert terminal is True, "a hash mismatch must be acked, not redelivered forever"
    assert doc.status == "failed"
    assert "integrity" in doc.processing_error


async def test_matching_hash_lets_processing_continue_past_the_check(monkeypatch):
    """Doesn't run the full pipeline -- proves the integrity check passed by
    asserting control reached parse_document (mocked to fail with a distinct,
    recognizable error) rather than being rejected by ContentIntegrityError."""
    content = b"bytes that match what was recorded at upload"
    doc = _Doc(content_sha256=hashlib.sha256(content).hexdigest())

    monkeypatch.setattr(processing, "Session", lambda _engine: _Session(doc))
    monkeypatch.setattr(processing, "get_engine", object)
    monkeypatch.setattr(processing, "AuditLogEntry", lambda **_kw: object())
    monkeypatch.setattr(processing, "get_object_store", lambda: _FakeObjectStore(content))

    def _boom(*_a, **_kw):
        raise ParsingError("reached parse_document")

    monkeypatch.setattr(processing, "parse_document", _boom)

    terminal = await processing.process_document(doc.id)

    assert terminal is True
    assert doc.status == "failed"
    assert doc.processing_error == "reached parse_document"


async def test_null_digest_skips_verification_for_pre_migration_rows(monkeypatch):
    """A document uploaded before this column existed has no digest to check
    against -- must not be treated as a mismatch."""
    content = b"whatever is in the object store for this legacy row"
    doc = _Doc(content_sha256=None)

    monkeypatch.setattr(processing, "Session", lambda _engine: _Session(doc))
    monkeypatch.setattr(processing, "get_engine", object)
    monkeypatch.setattr(processing, "AuditLogEntry", lambda **_kw: object())
    monkeypatch.setattr(processing, "get_object_store", lambda: _FakeObjectStore(content))

    def _boom(*_a, **_kw):
        raise ParsingError("reached parse_document")

    monkeypatch.setattr(processing, "parse_document", _boom)

    terminal = await processing.process_document(doc.id)

    assert terminal is True
    assert doc.processing_error == "reached parse_document"
