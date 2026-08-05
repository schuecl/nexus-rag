"""Issue #362: the re-embedding path #122/PR #130 shipped detection for but
not a fix. Covers app/reembed.py's batch logic with every external dependency
(DB session, object store, parser/chunker/embedder, vector store) faked --
same technique test_embedding_provenance.py and test_purge_race.py already
use for processing.py, since this module reuses the same pipeline pieces.
"""

from __future__ import annotations

import uuid

import pytest

from app import reembed
from app.chunking import Chunk
from app.parsing import ParsingError
from common.embedding_prefixes import embedding_identity
from common.qdrant_store import EMBEDDING_MODEL_KEY


class _Doc:
    def __init__(self, doc_id, classification="CUI", status="approved"):
        self.id = doc_id
        self.filename = "report.pdf"
        self.original_object_key = "documents/x/original"
        self.content_sha256 = None
        self.doc_type = "report"
        self.source_originator = "USAREUR-AF G2"
        self.classification = classification
        self.releasability = ["FVEY"]
        self.access_scope = ["USAREUR-AF"]
        self.status = status
        self.chunk_count = 0
        self.updated_at = None


class _FakeSession:
    def __init__(self, docs_by_id, rows):
        self._docs = docs_by_id
        self._rows = rows
        self.added: list = []
        self.committed = False

    def exec(self, _query):
        return self

    def all(self):
        return self._rows

    def get(self, _model, doc_id, **_kwargs):
        return self._docs.get(doc_id)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeObjectStore:
    def get(self, _key):
        return b"bytes"


class _FakeVectorStore:
    def __init__(self, existing_chunks_by_doc=None):
        self._existing = existing_chunks_by_doc or {}
        self.replace_calls: list = []

    def fetch_document_chunks(self, document_id, _classification):
        return self._existing.get(document_id, [])

    def replace_document_chunks(self, document_id, classification, points):
        self.replace_calls.append((document_id, classification, points))


def _wire(monkeypatch, *, docs, rows, existing_chunks_by_doc=None, chunks=None):
    """Common monkeypatching for a batch run. `docs` is {doc_id: _Doc},
    `rows` is the list of (id, classification) tuples the listing query
    returns. `chunks` (default: one chunk) is what chunk_sections yields."""
    session = _FakeSession(docs, rows)
    store = _FakeVectorStore(existing_chunks_by_doc)

    monkeypatch.setattr(reembed, "Session", lambda _engine: session)
    monkeypatch.setattr(reembed, "get_engine", lambda: None)
    monkeypatch.setattr(reembed, "get_object_store", _FakeObjectStore)
    monkeypatch.setattr(reembed, "get_store", lambda: store)
    monkeypatch.setattr(reembed, "parse_document", lambda _f, _c, _o=None: ["section"])
    monkeypatch.setattr(reembed, "captioning_enabled", lambda: False)
    monkeypatch.setattr(
        reembed, "chunk_sections", lambda _sections: chunks or [Chunk(text="hello", chunk_index=0)]
    )

    async def _fake_embed_texts(texts):
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(reembed, "embed_texts", _fake_embed_texts)
    monkeypatch.setattr(reembed, "embed_sparse", lambda texts: [object() for _ in texts])
    return session, store


class TestNeedsReembedding:
    def test_true_when_no_existing_chunks(self, monkeypatch):
        monkeypatch.setattr(reembed, "get_store", _FakeVectorStore)
        assert reembed._needs_reembedding(uuid.uuid4(), "CUI") is True

    def test_false_when_stamped_model_already_matches(self, monkeypatch):
        store = _FakeVectorStore(
            {"d1": [{EMBEDDING_MODEL_KEY: embedding_identity(reembed.EMBEDDING_MODEL)}]}
        )
        monkeypatch.setattr(reembed, "get_store", lambda: store)
        assert reembed._needs_reembedding("d1", "CUI") is False

    def test_true_when_stamped_with_bare_model_name_pre_392(self, monkeypatch):
        """A corpus embedded before #392 added prefixing carries the bare
        model name, which must now read as stale even though EMBEDDING_MODEL
        itself hasn't changed -- its passage vectors were never prefixed."""
        store = _FakeVectorStore({"d1": [{EMBEDDING_MODEL_KEY: reembed.EMBEDDING_MODEL}]})
        monkeypatch.setattr(reembed, "get_store", lambda: store)
        assert reembed._needs_reembedding("d1", "CUI") is True

    def test_true_when_stamped_model_differs(self, monkeypatch):
        store = _FakeVectorStore({"d1": [{EMBEDDING_MODEL_KEY: "some-old-model"}]})
        monkeypatch.setattr(reembed, "get_store", lambda: store)
        assert reembed._needs_reembedding("d1", "CUI") is True


class TestReembedClassifications:
    def test_processes_a_mismatched_document_and_preserves_its_tags(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id, classification="CUI", status="approved")
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={str(doc_id): [{EMBEDDING_MODEL_KEY: "stale-model"}]},
        )

        report = reembed.reembed_classifications(["CUI"])

        assert report.ok
        assert len(report.processed) == 1
        assert report.skipped_already_current == []
        assert len(store.replace_calls) == 1
        called_doc_id, classification, points = store.replace_calls[0]
        assert called_doc_id == str(doc_id)
        assert classification == "CUI"
        assert len(points) == 1
        point = points[0]
        assert point.id == str(uuid.uuid5(doc_id, "chunk:0"))
        assert point.payload["classification"] == "CUI"
        assert point.payload["releasability"] == ["FVEY"]
        assert point.payload["access_scope"] == ["USAREUR-AF"]
        assert point.payload["status"] == "approved"
        assert point.payload[EMBEDDING_MODEL_KEY] == embedding_identity(reembed.EMBEDDING_MODEL)

    def test_skips_a_document_already_on_the_current_model(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id)
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={
                str(doc_id): [{EMBEDDING_MODEL_KEY: embedding_identity(reembed.EMBEDDING_MODEL)}]
            },
        )

        report = reembed.reembed_classifications(["CUI"])

        assert report.processed == []
        assert len(report.skipped_already_current) == 1
        assert store.replace_calls == []

    def test_force_reprocesses_an_already_current_document(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id)
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={
                str(doc_id): [{EMBEDDING_MODEL_KEY: embedding_identity(reembed.EMBEDDING_MODEL)}]
            },
        )

        report = reembed.reembed_classifications(["CUI"], force=True)

        assert len(report.processed) == 1
        assert len(store.replace_calls) == 1

    def test_dry_run_reports_without_touching_the_store(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id)
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={str(doc_id): [{EMBEDDING_MODEL_KEY: "stale-model"}]},
        )

        report = reembed.reembed_classifications(["CUI"], dry_run=True)

        assert len(report.processed) == 1
        assert store.replace_calls == []

    def test_a_failure_on_one_document_does_not_stop_the_batch(self, monkeypatch):
        good_id, bad_id = uuid.uuid4(), uuid.uuid4()
        docs = {good_id: _Doc(good_id), bad_id: _Doc(bad_id)}
        _session, store = _wire(
            monkeypatch,
            docs=docs,
            rows=[(bad_id, "CUI"), (good_id, "CUI")],
            existing_chunks_by_doc={
                str(good_id): [{EMBEDDING_MODEL_KEY: "stale-model"}],
                str(bad_id): [{EMBEDDING_MODEL_KEY: "stale-model"}],
            },
        )

        calls = {"n": 0}

        def _parse(filename, content, ocr_status=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ParsingError("corrupt")
            return ["section"]

        monkeypatch.setattr(reembed, "parse_document", _parse)

        report = reembed.reembed_classifications(["CUI"])

        assert not report.ok
        assert len(report.failed) == 1
        assert len(report.processed) == 1
        assert len(store.replace_calls) == 1

    def test_missing_original_object_key_is_a_permanent_failure(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id)
        doc.original_object_key = None
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={str(doc_id): [{EMBEDDING_MODEL_KEY: "stale-model"}]},
        )

        report = reembed.reembed_classifications(["CUI"])

        assert not report.ok
        assert store.replace_calls == []

    def test_content_hash_mismatch_is_a_permanent_failure(self, monkeypatch):
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id)
        doc.content_sha256 = "0" * 64  # will never match sha256(b"bytes")
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={str(doc_id): [{EMBEDDING_MODEL_KEY: "stale-model"}]},
        )

        report = reembed.reembed_classifications(["CUI"])

        assert not report.ok
        assert "integrity" in next(iter(report.failed.values()))
        assert store.replace_calls == []

    def test_trailing_chunks_from_a_shorter_reparse_are_dropped_by_the_store_call(
        self, monkeypatch
    ):
        """Not this module's job to delete stale trailing points -- that's
        replace_document_chunks's contract (covered in
        tests/unit/common/test_reembed_chunks.py) -- but the point list this
        module builds must be exactly the *new* chunk count, so the store
        call has the right `len(points)` to sweep against."""
        doc_id = uuid.uuid4()
        doc = _Doc(doc_id)
        _session, store = _wire(
            monkeypatch,
            docs={doc_id: doc},
            rows=[(doc_id, "CUI")],
            existing_chunks_by_doc={str(doc_id): [{EMBEDDING_MODEL_KEY: "stale-model"}]},
            chunks=[Chunk(text="only one now", chunk_index=0)],
        )

        reembed.reembed_classifications(["CUI"])

        _, _, points = store.replace_calls[0]
        assert len(points) == 1


@pytest.mark.parametrize("classifications", [None, ["CUI"]])
def test_reembed_classifications_accepts_none_or_a_list(monkeypatch, classifications):
    """None means "every classification with an eligible document" -- just
    confirm both call shapes reach the listing query without raising."""
    monkeypatch.setattr(reembed, "Session", lambda _engine: _FakeSession({}, []))
    monkeypatch.setattr(reembed, "get_engine", lambda: None)

    report = reembed.reembed_classifications(classifications)

    assert report.processed == []
    assert report.failed == {}
