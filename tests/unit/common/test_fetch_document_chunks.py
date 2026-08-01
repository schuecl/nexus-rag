"""Issue #284: fetch_document_chunks, the vector-store seam method the new
curator content view (`GET /curate/{doc_id}/content`) reads chunk text
through. Covers all three layers: the Qdrant free function (paginate +
sort), the QdrantStore adapter (delegates to it), and MilvusStore (single
query() + sort, no pagination since Milvus has no per-document chunk-count
concern here -- mirrors update_document_payload's own bounded `limit`).

Fake clients throughout, same technique as test_qdrant_backend_fanout.py --
no live Qdrant/Milvus needed, and MilvusStore.fetch_document_chunks never
touches pymilvus directly (only `_client()` does, which is monkeypatched
here), so this runs without pymilvus installed.
"""

from __future__ import annotations

from common import milvus_store, qdrant_backend
from common.qdrant_store import classification_collection_name, fetch_document_chunks


class _Point:
    def __init__(self, payload):
        self.payload = payload


class FakeScrollClient:
    """Fakes just enough of QdrantClient for fetch_document_chunks:
    collection_exists + a paginated scroll keyed by collection name."""

    def __init__(self, *, pages_by_collection, existing=None):
        self._pages_by_collection = pages_by_collection
        self._existing = existing if existing is not None else set(pages_by_collection)
        self.scroll_calls = []

    def collection_exists(self, name):
        return name in self._existing

    def scroll(self, *, collection_name, scroll_filter, limit, with_payload, with_vectors, offset):
        del scroll_filter, limit, with_payload, with_vectors
        self.scroll_calls.append((collection_name, offset))
        pages = self._pages_by_collection[collection_name]
        page_index = 0 if offset is None else offset
        points, next_offset = pages[page_index]
        return points, next_offset


class TestQdrantStoreFetchDocumentChunksFunction:
    def test_returns_empty_list_when_collection_does_not_exist(self):
        client = FakeScrollClient(pages_by_collection={}, existing=set())
        assert fetch_document_chunks(client, "doc-1", "CUI") == []

    def test_sorts_by_chunk_index_regardless_of_scroll_order(self):
        name = classification_collection_name("CUI")
        client = FakeScrollClient(
            pages_by_collection={
                name: [
                    (
                        [
                            _Point({"chunk_index": 2, "text": "third"}),
                            _Point({"chunk_index": 0, "text": "first"}),
                        ],
                        None,
                    )
                ]
            }
        )

        chunks = fetch_document_chunks(client, "doc-1", "CUI")

        assert [c["chunk_index"] for c in chunks] == [0, 2]

    def test_paginates_across_multiple_scroll_pages(self):
        name = classification_collection_name("CUI")
        client = FakeScrollClient(
            pages_by_collection={
                name: [
                    ([_Point({"chunk_index": 0, "text": "first"})], 1),
                    ([_Point({"chunk_index": 1, "text": "second"})], None),
                ]
            }
        )

        chunks = fetch_document_chunks(client, "doc-1", "CUI")

        assert [c["text"] for c in chunks] == ["first", "second"]
        assert client.scroll_calls == [(name, None), (name, 1)]


class TestQdrantStoreAdapterDelegates:
    def test_fetch_document_chunks_delegates_to_get_qdrant_client(self, monkeypatch):
        name = classification_collection_name("SECRET")
        client = FakeScrollClient(
            pages_by_collection={name: [([_Point({"chunk_index": 0, "text": "x"})], None)]}
        )
        monkeypatch.setattr(qdrant_backend, "get_qdrant_client", lambda: client)

        result = qdrant_backend.QdrantStore().fetch_document_chunks("doc-1", "SECRET")

        assert result == [{"chunk_index": 0, "text": "x"}]


class FakeMilvusClient:
    def __init__(self, rows):
        self._rows = rows
        self.query_calls = []

    def query(self, *, collection_name, filter, output_fields, limit):
        self.query_calls.append((collection_name, filter, output_fields, limit))
        return self._rows


class TestMilvusStoreFetchDocumentChunks:
    def test_sorts_by_chunk_index(self, monkeypatch):
        client = FakeMilvusClient(
            rows=[
                {"payload": {"chunk_index": 3, "text": "later"}},
                {"payload": {"chunk_index": 1, "text": "earlier"}},
            ]
        )
        monkeypatch.setattr(milvus_store, "_client", lambda: client)

        # classification is unused for Milvus (#229 not implemented there) --
        # passing an arbitrary value must not affect the query.
        chunks = milvus_store.MilvusStore().fetch_document_chunks("doc-1", "CUI")

        assert [c["chunk_index"] for c in chunks] == [1, 3]
        assert client.query_calls[0][0] == milvus_store.MILVUS_COLLECTION

    def test_no_rows_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(milvus_store, "_client", lambda: FakeMilvusClient(rows=[]))

        assert milvus_store.MilvusStore().fetch_document_chunks("doc-1", "CUI") == []
