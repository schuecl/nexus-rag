"""Issue #229: QdrantStore.hybrid_query fans out over one collection per
classification the caller is allowed to see, then fuses the per-collection
results by rank (common.vector_store.fuse_ranked) since scores from
different collections aren't comparable -- see qdrant_backend.py's module
docstring.

Uses a fake QdrantClient rather than qdrant_store.get_qdrant_client's real
lru_cache singleton so each test starts clean.
"""

from __future__ import annotations

import pytest
from qdrant_client.http.exceptions import UnexpectedResponse

from common import qdrant_backend
from common.claims import UserClaims
from common.qdrant_store import classification_collection_name
from common.vector_store import VectorStoreUnavailable


def _claims() -> UserClaims:
    return UserClaims(
        sub="user-1",
        preferred_username="user-1",
        groups=["analysts"],
        org="USAREUR-AF",
        rag_roles=["rag-query", "rag-clearance:SECRET", "rag-releasability:NONE"],
    )


class _Hit:
    def __init__(self, id_, score, payload=None):
        self.id = id_
        self.score = score
        self.payload = payload or {}


class _QueryResult:
    def __init__(self, points):
        self.points = points


class _Collection:
    def __init__(self, name):
        self.name = name


class _Collections:
    def __init__(self, names):
        self.collections = [_Collection(n) for n in names]


class FakeClient:
    def __init__(self, *, hits_by_collection=None, existing=None, raises_for=None):
        self._hits_by_collection = hits_by_collection or {}
        self._existing = existing if existing is not None else set(self._hits_by_collection)
        self._raises_for = raises_for or set()
        self._queries = []

    def collection_exists(self, name):
        return name in self._existing

    def get_collections(self):
        return _Collections(sorted(self._existing))

    def query_points(self, *, collection_name, **kwargs):
        self._queries.append((collection_name, kwargs))
        if collection_name in self._raises_for:
            raise UnexpectedResponse(
                status_code=503, reason_phrase="down", content=b"", headers=None
            )
        return _QueryResult(self._hits_by_collection.get(collection_name, []))


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    holder: dict[str, FakeClient] = {}

    def _get(*, hits_by_collection=None, existing=None, raises_for=None):
        holder["client"] = FakeClient(
            hits_by_collection=hits_by_collection, existing=existing, raises_for=raises_for
        )
        return holder["client"]

    def _install(**kwargs):
        client = _get(**kwargs)
        monkeypatch.setattr(qdrant_backend, "get_qdrant_client", lambda: client)
        return client

    yield _install


class TestHybridQueryFanOut:
    def test_only_allowed_and_existing_collections_are_queried(self, _client):
        cui = classification_collection_name("CUI")
        _client(hits_by_collection={cui: [_Hit("c1", 0.9)]}, existing={cui})
        store = qdrant_backend.QdrantStore()

        hits = store.hybrid_query(
            dense=[0.1],
            sparse=None,
            claims=_claims(),
            allowed_classifications=["CUI", "SECRET"],  # SECRET has no collection yet
            limit=10,
        )

        assert [h.id for h in hits] == ["c1"]

    def test_results_from_several_collections_are_rank_fused(self, _client):
        cui = classification_collection_name("CUI")
        secret = classification_collection_name("SECRET")
        _client(
            hits_by_collection={
                cui: [_Hit("cui-1", 0.99), _Hit("cui-2", 0.5)],
                secret: [_Hit("secret-1", 0.4)],
            },
            existing={cui, secret},
        )
        store = qdrant_backend.QdrantStore()

        hits = store.hybrid_query(
            dense=[0.1],
            sparse=None,
            claims=_claims(),
            allowed_classifications=["CUI", "SECRET"],
            limit=10,
        )

        # Every candidate from every queried collection survives the fusion.
        assert {h.id for h in hits} == {"cui-1", "cui-2", "secret-1"}
        # Rank 1 in its own collection outranks rank 2 in another.
        ids = [h.id for h in hits]
        assert ids.index("cui-1") < ids.index("cui-2")

    def test_no_allowed_classifications_returns_nothing(self, _client):
        _client()
        store = qdrant_backend.QdrantStore()

        hits = store.hybrid_query(
            dense=[0.1], sparse=None, claims=_claims(), allowed_classifications=[], limit=10
        )

        assert hits == []

    def test_backend_failure_on_any_collection_raises_unavailable(self, _client):
        cui = classification_collection_name("CUI")
        _client(existing={cui}, raises_for={cui})
        store = qdrant_backend.QdrantStore()

        with pytest.raises(VectorStoreUnavailable):
            store.hybrid_query(
                dense=[0.1],
                sparse=None,
                claims=_claims(),
                allowed_classifications=["CUI"],
                limit=10,
            )


class TestFindSimilarApproved:
    """Issue #307 Phase 2: unlike hybrid_query, this fans out over *every*
    existing classification collection -- there's no caller claims/allowed
    list to scope it to (see the Protocol docstring)."""

    def test_searches_every_existing_collection(self, _client):
        cui = classification_collection_name("CUI")
        secret = classification_collection_name("SECRET")
        client = _client(
            hits_by_collection={
                cui: [_Hit("cui-1", 0.9, {"classification": "CUI"})],
                secret: [_Hit("secret-1", 0.8, {"classification": "SECRET"})],
            },
            existing={cui, secret},
        )
        store = qdrant_backend.QdrantStore()

        hits = store.find_similar_approved(dense=[0.1], limit=10)

        assert {h.id for h in hits} == {"cui-1", "secret-1"}
        assert {name for name, _ in client._queries} == {cui, secret}

    def test_results_are_ranked_by_real_score_across_collections_not_rrf(self, _client):
        # A dense-only query against one collection returns a real COSINE
        # score, comparable across collections (unlike hybrid_query's
        # dense+sparse fusion, whose per-collection BM25 IDF isn't) -- so
        # this must sort by that score directly, not apply rank-only RRF
        # (fuse_ranked), which would treat two different collections' rank-1
        # hits as equally good regardless of how far apart their true scores
        # are. Caught live against a real Qdrant: a 0.98-similarity
        # near-duplicate was buried behind unrelated 0.6-similarity hits that
        # merely happened to also rank 1 in their own (smaller) collection.
        cui = classification_collection_name("CUI")
        secret = classification_collection_name("SECRET")
        _client(
            hits_by_collection={
                cui: [
                    _Hit("cui-1", 0.60, {"classification": "CUI"}),
                    _Hit("cui-2", 0.58, {"classification": "CUI"}),
                ],
                secret: [_Hit("secret-1", 0.98, {"classification": "SECRET"})],
            },
            existing={cui, secret},
        )
        store = qdrant_backend.QdrantStore()

        hits = store.find_similar_approved(dense=[0.1], limit=2)

        assert [h.id for h in hits] == ["secret-1", "cui-1"]

    def test_status_approved_filter_is_applied(self, _client):
        cui = classification_collection_name("CUI")
        client = _client(hits_by_collection={cui: []}, existing={cui})
        store = qdrant_backend.QdrantStore()

        store.find_similar_approved(dense=[0.1], limit=10)

        _, kwargs = client._queries[0]
        query_filter = kwargs["query_filter"]
        assert any(c.key == "status" and c.match.value == "approved" for c in query_filter.must)

    def test_exclude_document_id_is_applied_as_must_not(self, _client):
        cui = classification_collection_name("CUI")
        client = _client(hits_by_collection={cui: []}, existing={cui})
        store = qdrant_backend.QdrantStore()

        store.find_similar_approved(dense=[0.1], limit=10, exclude_document_id="doc-1")

        _, kwargs = client._queries[0]
        query_filter = kwargs["query_filter"]
        assert query_filter.must_not[0].key == "document_id"
        assert query_filter.must_not[0].match.value == "doc-1"

    def test_no_exclude_document_id_omits_must_not(self, _client):
        cui = classification_collection_name("CUI")
        client = _client(hits_by_collection={cui: []}, existing={cui})
        store = qdrant_backend.QdrantStore()

        store.find_similar_approved(dense=[0.1], limit=10)

        _, kwargs = client._queries[0]
        assert not kwargs["query_filter"].must_not

    def test_no_collections_returns_nothing(self, _client):
        _client()
        store = qdrant_backend.QdrantStore()

        assert store.find_similar_approved(dense=[0.1], limit=10) == []

    def test_backend_failure_raises_unavailable(self, _client):
        cui = classification_collection_name("CUI")
        _client(existing={cui}, raises_for={cui})
        store = qdrant_backend.QdrantStore()

        with pytest.raises(VectorStoreUnavailable):
            store.find_similar_approved(dense=[0.1], limit=10)


class TestAccessFilterSummary:
    def test_collections_are_reported_for_every_allowed_classification(self, _client):
        _client()
        store = qdrant_backend.QdrantStore()

        summary = store.access_filter_summary(_claims(), ["CUI", "SECRET"])

        assert summary["collections"] == [
            classification_collection_name("CUI"),
            classification_collection_name("SECRET"),
        ]
