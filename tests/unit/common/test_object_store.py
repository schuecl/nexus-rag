"""Unit tests for common.object_store -- NFR-12 durable original-file storage.
Filesystem backend exercised for real under tmp_path; S3 backend exercised
against a stubbed boto3 client; backend selection via env.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from common.object_store import (
    FilesystemObjectStore,
    S3ObjectStore,
    document_object_key,
    get_object_store,
)


class TestFilesystemObjectStore:
    def test_put_get_delete_round_trip(self, tmp_path):
        store = FilesystemObjectStore(str(tmp_path))
        store.put("documents/abc/original", b"file-bytes")
        assert store.get("documents/abc/original") == b"file-bytes"
        store.delete("documents/abc/original")
        with pytest.raises(FileNotFoundError):
            store.get("documents/abc/original")

    def test_delete_missing_key_is_silent(self, tmp_path):
        FilesystemObjectStore(str(tmp_path)).delete("never/existed")

    def test_nested_key_creates_parents(self, tmp_path):
        store = FilesystemObjectStore(str(tmp_path))
        store.put("a/b/c/d", b"x")
        assert (tmp_path / "a" / "b" / "c" / "d").read_bytes() == b"x"

    @pytest.mark.parametrize("key", ["../escape", "documents/../../escape",
                                     "/absolute/path", ".."])
    def test_path_traversal_keys_rejected(self, tmp_path, key):
        store = FilesystemObjectStore(str(tmp_path))
        with pytest.raises(ValueError, match="invalid object key"):
            store.put(key, b"x")
        with pytest.raises(ValueError, match="invalid object key"):
            store.get(key)


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        del self.objects[(Bucket, Key)]


class TestS3ObjectStore:
    @pytest.fixture
    def fake_boto3(self, monkeypatch):
        client = _FakeS3Client()
        module = ModuleType("boto3")
        module.client = lambda *args, **kwargs: client
        monkeypatch.setitem(sys.modules, "boto3", module)
        return client

    def _store(self):
        return S3ObjectStore(endpoint_url="http://minio:9000", bucket="nexus",
                             access_key="ak", secret_key="sk")

    def test_round_trip(self, fake_boto3):
        store = self._store()
        store.put("documents/abc/original", b"s3-bytes")
        assert store.get("documents/abc/original") == b"s3-bytes"
        store.delete("documents/abc/original")
        assert ("nexus", "documents/abc/original") not in fake_boto3.objects


class TestGetObjectStore:
    def setup_method(self):
        get_object_store.cache_clear()

    def teardown_method(self):
        get_object_store.cache_clear()

    def test_filesystem_is_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OBJECT_STORE_BACKEND", raising=False)
        monkeypatch.setenv("OBJECT_STORE_PATH", str(tmp_path))
        assert isinstance(get_object_store(), FilesystemObjectStore)

    def test_unknown_backend_rejected(self, monkeypatch):
        monkeypatch.setenv("OBJECT_STORE_BACKEND", "carrier-pigeon")
        with pytest.raises(ValueError, match="unknown OBJECT_STORE_BACKEND"):
            get_object_store()

    def test_s3_backend_requires_config(self, monkeypatch):
        monkeypatch.setenv("OBJECT_STORE_BACKEND", "s3")
        for var in ("OBJECT_STORE_S3_ENDPOINT", "OBJECT_STORE_S3_BUCKET",
                    "OBJECT_STORE_S3_ACCESS_KEY", "OBJECT_STORE_S3_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(KeyError):
            get_object_store()


class TestDocumentObjectKey:
    def test_canonical_format(self):
        doc_id = uuid4 = "123e4567-e89b-12d3-a456-426614174000"
        assert document_object_key(doc_id) == f"documents/{uuid4}/original"

    def test_no_filename_component(self):
        # Keys are id-based only -- an uploader-controlled filename must never
        # become part of the storage path.
        assert ".." not in document_object_key("some-id")
