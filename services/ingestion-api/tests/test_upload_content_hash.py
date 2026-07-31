"""Issue #285: content_sha256 is computed over the exact bytes submit_document
spools (riding along the #107 streaming read, see test_upload_size_guard.py
for that guard's own coverage), stored on the Document row, and included in
the document.submit audit entry -- the anchor ingestion-worker's
re-verification check (services/ingestion-worker/tests/test_content_hash.py)
verifies against.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.datastructures import UploadFile

from app.routes import upload
from common.claims import UserClaims
from common.models import AuditLogEntry, ClassificationLevel, Document

UPLOADER = UserClaims(
    sub="uploader-sub",
    preferred_username="alice-ingest",
    org="USAREUR-AF",
    rag_roles=["rag-ingest", "rag-clearance:SECRET", "rag-releasability:NONE"],
)


class _FakeObjectStore:
    def __init__(self):
        self.puts: dict[str, bytes] = {}

    def put(self, key: str, content: bytes) -> None:
        self.puts[key] = content

    def get(self, key: str) -> bytes:
        return self.puts[key]

    def delete(self, key: str) -> None:
        self.puts.pop(key, None)


class _FakeJetStream:
    pass


class _FakeAppState:
    def __init__(self):
        self.jetstream = _FakeJetStream()


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


class _FakeRequest:
    def __init__(self):
        self.app = _FakeApp()


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


@pytest.fixture
def store(monkeypatch):
    fake = _FakeObjectStore()
    monkeypatch.setattr(upload, "get_object_store", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _no_op_queue_publish(monkeypatch):
    async def _publish(_js, _value):
        return None

    monkeypatch.setattr(upload, "publish_ingestion_job", _publish)
    monkeypatch.setattr(upload, "mark_published", lambda _doc_id: None)


def _upload_file(data: bytes, filename: str = "report.txt") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, size=len(data))


async def test_submit_stores_sha256_of_the_uploaded_bytes(session, store):
    contents = b"the quick brown fox jumps over the lazy dog"
    expected = hashlib.sha256(contents).hexdigest()

    doc = await upload.submit_document(
        request=_FakeRequest(),
        file=_upload_file(contents),
        classification="CUI",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="USAREUR-AF",
        doc_type="report",
        program_community=None,
        effective_date=None,
        supersedes_document_id=None,
        user=UPLOADER,
        session=session,
        _csrf=None,
    )

    assert doc.content_sha256 == expected
    # And durably on the row, not just the in-memory object returned.
    persisted = session.get(Document, doc.id)
    assert persisted is not None
    assert persisted.content_sha256 == expected
    # The bytes actually written to the object store hash to the same digest
    # -- this is the value ingestion-worker's re-verification is checked
    # against, so it has to be the real stored bytes, not something computed
    # from a separate read.
    assert hashlib.sha256(store.puts[doc.original_object_key]).hexdigest() == expected


async def test_submit_audit_entry_carries_the_digest(session, store):
    contents = b"some other document content"
    expected = hashlib.sha256(contents).hexdigest()

    doc = await upload.submit_document(
        request=_FakeRequest(),
        file=_upload_file(contents),
        classification="CUI",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="USAREUR-AF",
        doc_type="report",
        program_community=None,
        effective_date=None,
        supersedes_document_id=None,
        user=UPLOADER,
        session=session,
        _csrf=None,
    )

    entry = session.exec(
        select(AuditLogEntry).where(
            AuditLogEntry.action == "document.submit", AuditLogEntry.target_id == str(doc.id)
        )
    ).one()
    assert entry.detail["content_sha256"] == expected


async def test_two_uploads_with_identical_bytes_get_the_same_digest(session, store):
    contents = b"identical content"

    first = await upload.submit_document(
        request=_FakeRequest(),
        file=_upload_file(contents, filename="a.txt"),
        classification="CUI",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="USAREUR-AF",
        doc_type="report",
        program_community=None,
        effective_date=None,
        supersedes_document_id=None,
        user=UPLOADER,
        session=session,
        _csrf=None,
    )
    second = await upload.submit_document(
        request=_FakeRequest(),
        file=_upload_file(contents, filename="b.txt"),
        classification="CUI",
        releasability='["NONE"]',
        access_scope='["ALL_AUTHENTICATED"]',
        source_originator="USAREUR-AF",
        doc_type="report",
        program_community=None,
        effective_date=None,
        supersedes_document_id=None,
        user=UPLOADER,
        session=session,
        _csrf=None,
    )

    assert first.content_sha256 == second.content_sha256
    assert first.id != second.id
