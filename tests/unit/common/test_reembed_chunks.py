"""Issue #362: replace_document_chunks, the vector-store seam method
app/reembed.py (ingestion-worker) writes freshly re-embedded chunks through.
Covers all three layers: the Qdrant free function (upsert-new, then sweep
stale trailing points), the QdrantStore adapter (delegates to it), and
MilvusStore (upsert, then query+delete since chunk_index lives only in the
JSON payload there).

Fake clients throughout, same technique as test_fetch_document_chunks.py --
no live Qdrant/Milvus needed.
"""

from __future__ import annotations

from qdrant_client.models import PointStruct, SparseVector

from common import milvus_store, qdrant_backend
from common.qdrant_store import (
    DENSE_VECTOR,
    classification_collection_name,
    replace_document_chunks,
)


class _FakePoint:
    def __init__(self, id_, vector, payload):
        self.id = id_
        self.vector = vector
        self.payload = payload


class FakeQdrantClient:
    """Interprets exactly the Filter shape replace_document_chunks builds: a
    document_id match plus a chunk_index >= N range, both under `must`."""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.delete_calls: list[tuple[str, str, int]] = []

    def upsert(self, *, collection_name, points):
        bucket = self.collections.setdefault(collection_name, {})
        for p in points:
            bucket[p.id] = _FakePoint(p.id, p.vector, dict(p.payload or {}))

    def delete(self, *, collection_name, points_selector):
        bucket = self.collections.get(collection_name, {})
        conditions = points_selector.filter.must
        doc_id = next(c.match.value for c in conditions if c.match is not None)
        min_chunk_index = next(c.range.gte for c in conditions if c.range is not None)
        self.delete_calls.append((collection_name, doc_id, min_chunk_index))
        stale = [
            pid
            for pid, p in bucket.items()
            if p.payload.get("document_id") == doc_id
            and p.payload.get("chunk_index", 0) >= min_chunk_index
        ]
        for pid in stale:
            del bucket[pid]


def _point(id_, document_id, chunk_index) -> PointStruct:
    return PointStruct(
        id=id_,
        vector={DENSE_VECTOR: [0.1, 0.2], "bm25": SparseVector(indices=[1], values=[0.5])},
        payload={"document_id": document_id, "chunk_index": chunk_index},
    )


class TestReplaceDocumentChunksFunction:
    def test_new_points_overwrite_matching_old_ones_in_place(self):
        client = FakeQdrantClient()
        name = classification_collection_name("CUI")
        client.upsert(collection_name=name, points=[_point("c0", "doc-1", 0)])

        replace_document_chunks(client, "doc-1", "CUI", [_point("c0", "doc-1", 0)])

        assert set(client.collections[name]) == {"c0"}

    def test_stale_trailing_points_beyond_the_new_count_are_deleted(self):
        client = FakeQdrantClient()
        name = classification_collection_name("CUI")
        # Old chunking produced 3 chunks; new re-parse only produces 1.
        client.upsert(
            collection_name=name,
            points=[_point("c0", "doc-1", 0), _point("c1", "doc-1", 1), _point("c2", "doc-1", 2)],
        )

        replace_document_chunks(client, "doc-1", "CUI", [_point("c0", "doc-1", 0)])

        assert set(client.collections[name]) == {"c0"}

    def test_new_points_beyond_the_old_count_are_kept_not_swept(self):
        client = FakeQdrantClient()
        name = classification_collection_name("CUI")
        client.upsert(collection_name=name, points=[_point("c0", "doc-1", 0)])

        replace_document_chunks(
            client, "doc-1", "CUI", [_point("c0", "doc-1", 0), _point("c1", "doc-1", 1)]
        )

        assert set(client.collections[name]) == {"c0", "c1"}

    def test_a_different_documents_points_are_never_touched(self):
        client = FakeQdrantClient()
        name = classification_collection_name("CUI")
        client.upsert(
            collection_name=name,
            points=[_point("c0", "doc-1", 0), _point("other", "doc-2", 0)],
        )

        replace_document_chunks(client, "doc-1", "CUI", [_point("c0", "doc-1", 0)])

        assert "other" in client.collections[name]


class TestQdrantStoreAdapterDelegates:
    def test_replace_document_chunks_delegates_to_get_qdrant_client(self, monkeypatch):
        client = FakeQdrantClient()
        monkeypatch.setattr(qdrant_backend, "get_qdrant_client", lambda: client)
        from common.vector_store import ChunkPoint

        point = ChunkPoint(
            id="c0",
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[1], values=[0.5]),
            payload={"document_id": "doc-1", "chunk_index": 0},
        )

        qdrant_backend.QdrantStore().replace_document_chunks("doc-1", "CUI", [point])

        name = classification_collection_name("CUI")
        assert "c0" in client.collections[name]


class FakeMilvusClient:
    def __init__(self, rows):
        self._rows = rows
        self.upsert_calls: list = []
        self.delete_calls: list = []

    def upsert(self, *, collection_name, data):
        del collection_name
        self.upsert_calls.append(data)

    def query(self, *, collection_name, filter, output_fields, limit):
        del collection_name, filter, output_fields, limit
        return self._rows

    def delete(self, *, collection_name, filter):
        del collection_name
        self.delete_calls.append(filter)


class TestMilvusStoreReplaceDocumentChunks:
    def test_upserts_new_points(self, monkeypatch):
        client = FakeMilvusClient(rows=[])
        monkeypatch.setattr(milvus_store, "_client", lambda: client)
        from common.vector_store import ChunkPoint

        point = ChunkPoint(
            id="c0",
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[1], values=[0.5]),
            payload={"document_id": "doc-1", "chunk_index": 0},
        )

        milvus_store.MilvusStore().replace_document_chunks("doc-1", "CUI", [point])

        assert len(client.upsert_calls) == 1
        assert client.upsert_calls[0][0]["id"] == "c0"

    def test_deletes_stale_trailing_rows_by_id(self, monkeypatch):
        client = FakeMilvusClient(
            rows=[
                {"id": "c0", "payload": {"chunk_index": 0}},
                {"id": "c1", "payload": {"chunk_index": 1}},
            ]
        )
        monkeypatch.setattr(milvus_store, "_client", lambda: client)
        from common.vector_store import ChunkPoint

        point = ChunkPoint(
            id="c0",
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[1], values=[0.5]),
            payload={"document_id": "doc-1", "chunk_index": 0},
        )

        milvus_store.MilvusStore().replace_document_chunks("doc-1", "CUI", [point])

        assert len(client.delete_calls) == 1
        assert '"c1"' in client.delete_calls[0]
        assert '"c0"' not in client.delete_calls[0]

    def test_no_stale_rows_means_no_delete_call(self, monkeypatch):
        client = FakeMilvusClient(rows=[{"id": "c0", "payload": {"chunk_index": 0}}])
        monkeypatch.setattr(milvus_store, "_client", lambda: client)
        from common.vector_store import ChunkPoint

        point = ChunkPoint(
            id="c0",
            dense=[0.1, 0.2],
            sparse=SparseVector(indices=[1], values=[0.5]),
            payload={"document_id": "doc-1", "chunk_index": 0},
        )

        milvus_store.MilvusStore().replace_document_chunks("doc-1", "CUI", [point])

        assert client.delete_calls == []
