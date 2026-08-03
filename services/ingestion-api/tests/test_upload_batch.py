"""Issue #356 (FR-1's "one or more documents", FR-34): POST /documents/batch
accepts N files sharing one metadata payload. Metadata is validated against
the caller's claims exactly once; each file is then stored/recorded/queued
independently through the same _ingest_one_file path submit_document uses,
so one file's rejection doesn't fail the rest of the batch and each
resulting document still gets its own curator review.
"""

from __future__ import annotations

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


class _FakeRequest:
    class _App:
        class _State:
            jetstream = None

        state = _State()

    app = _App()


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


def _file(data: bytes, filename: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename, size=len(data))


async def _submit_batch(session, files, **overrides):
    kwargs = {
        "request": _FakeRequest(),
        "files": files,
        "classification": "CUI",
        "releasability": '["NONE"]',
        "access_scope": '["ALL_AUTHENTICATED"]',
        "source_originator": "USAREUR-AF",
        "doc_type": "report",
        "program_community": None,
        "effective_date": None,
        "user": UPLOADER,
        "session": session,
        "_csrf": None,
    }
    kwargs.update(overrides)
    return await upload.submit_documents_batch(**kwargs)


class TestBatchHappyPath:
    async def test_all_files_are_accepted_and_stored_independently(self, session, store):
        files = [
            _file(b"first document body", "a.txt"),
            _file(b"second document body", "b.txt"),
            _file(b"third document body", "c.txt"),
        ]

        results = await _submit_batch(session, files)

        assert len(results) == 3
        assert all(item.accepted for item in results)
        ids = {item.document.id for item in results}
        assert len(ids) == 3  # each file got its own Document row

        for item in results:
            persisted = session.get(Document, item.document.id)
            assert persisted is not None
            assert persisted.status == "queued"
            assert persisted.classification == "CUI"
            assert store.puts[persisted.original_object_key]

    async def test_every_item_document_survives_json_serialization(self, session, store):
        # Regression test: attribute access on item.document (as the other
        # tests in this file do) transparently re-fetches expired attributes
        # through the still-open session, so it can't see this bug. FastAPI's
        # actual response path serializes with jsonable_encoder instead,
        # which reads each SQLModel's __dict__ directly -- before this was
        # fixed with .model_copy() in submit_documents_batch, every batch
        # item but the last shared its `document` by reference with the live
        # ORM object, so a later file's session.commit() (expire_on_commit)
        # emptied the earlier ones' __dict__ and they serialized as `{}`.
        from fastapi.encoders import jsonable_encoder

        files = [
            _file(b"first document body", "a.txt"),
            _file(b"second document body", "b.txt"),
            _file(b"third document body", "c.txt"),
        ]

        results = await _submit_batch(session, files)
        encoded = jsonable_encoder(results)

        assert len(encoded) == 3
        for item in encoded:
            assert item["document"]["id"], f"{item['filename']} serialized with an empty document"
            assert item["document"]["status"] == "queued"

    async def test_each_accepted_file_gets_its_own_audit_entry(self, session, store):
        files = [_file(b"one", "one.txt"), _file(b"two", "two.txt")]

        results = await _submit_batch(session, files)

        entries = session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.submit")
        ).all()
        assert {e.target_id for e in entries} == {str(item.document.id) for item in results}

    async def test_shared_metadata_is_applied_to_every_document(self, session, store):
        files = [_file(b"one", "one.txt"), _file(b"two", "two.txt")]

        results = await _submit_batch(
            session, files, access_scope='["Signal-Corps"]', doc_type="regulation"
        )

        for item in results:
            assert item.document.access_scope == ["Signal-Corps"]
            assert item.document.doc_type == "regulation"


class TestBatchPartialFailure:
    async def test_one_bad_file_does_not_block_the_rest_of_the_batch(self, session, store):
        files = [
            _file(b"good content", "good.txt"),
            _file(b"", "empty.txt"),  # rejected: empty file
            _file(b"more good content", "also-good.txt"),
        ]

        results = await _submit_batch(session, files)

        assert len(results) == 3
        by_name = {item.filename: item for item in results}
        assert by_name["good.txt"].accepted
        assert by_name["also-good.txt"].accepted
        assert not by_name["empty.txt"].accepted
        assert by_name["empty.txt"].document is None
        assert "empty file" in by_name["empty.txt"].detail

        # The accepted files are still durably persisted despite the failure
        # sitting between them in the batch.
        assert session.get(Document, by_name["good.txt"].document.id) is not None
        assert session.get(Document, by_name["also-good.txt"].document.id) is not None

    async def test_unsupported_file_type_is_reported_per_file(self, session, store):
        files = [
            _file(b"good content", "good.txt"),
            _file(b"not a real executable but wrong extension", "bad.exe"),
        ]

        results = await _submit_batch(session, files)

        by_name = {item.filename: item for item in results}
        assert by_name["good.txt"].accepted
        assert not by_name["bad.exe"].accepted


class TestBatchSharedMetadataValidation:
    async def test_metadata_rejected_by_claims_fails_the_whole_batch_before_any_file_is_touched(
        self, session, store
    ):
        files = [_file(b"one", "one.txt"), _file(b"two", "two.txt")]

        with pytest.raises(Exception) as excinfo:
            await _submit_batch(session, files, classification="TOP SECRET")

        assert excinfo.value.status_code == 403  # type: ignore[attr-defined]
        assert session.exec(select(Document)).first() is None
        assert store.puts == {}

    async def test_invalid_metadata_json_fails_before_any_file_is_touched(self, session, store):
        files = [_file(b"one", "one.txt")]

        with pytest.raises(Exception) as excinfo:
            await _submit_batch(session, files, releasability="not-json")

        assert excinfo.value.status_code == 400  # type: ignore[attr-defined]
        assert store.puts == {}


class TestBatchGuards:
    async def test_empty_file_list_is_rejected(self, session, store):
        with pytest.raises(Exception) as excinfo:
            await _submit_batch(session, [])

        assert excinfo.value.status_code == 400  # type: ignore[attr-defined]

    async def test_batch_over_the_file_count_limit_is_rejected(self, session, store, monkeypatch):
        monkeypatch.setattr(upload, "MAX_BATCH_FILES", 2)
        files = [_file(b"one", "1.txt"), _file(b"two", "2.txt"), _file(b"three", "3.txt")]

        with pytest.raises(Exception) as excinfo:
            await _submit_batch(session, files)

        assert excinfo.value.status_code == 400  # type: ignore[attr-defined]
        assert store.puts == {}


class TestBatchDoesNotAcceptSupersession:
    def test_batch_endpoint_has_no_supersedes_parameter(self):
        import inspect

        params = inspect.signature(upload.submit_documents_batch).parameters
        assert "supersedes_document_id" not in params
