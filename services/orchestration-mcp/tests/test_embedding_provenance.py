"""Coverage for issue #122: a change to EMBEDDING_MODEL must not silently
degrade dense retrieval.

The failure this guards is specifically a *quiet* one. Qdrant compares
whatever vectors it is handed, `ensure_collection` only acts when the
collection is absent, and a replacement model with the same dimensionality
(768 is near-universal) writes straight into the existing collection. The
dense leg then contributes noise while BM25 keeps returning plausible keyword
matches and RRF fuses them, so nothing errors and results still look
reasonable.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app import rag_search
from common import qdrant_store


class _Claims:
    sub = "u1"
    preferred_username = "bob-query"
    clearance = "SECRET"
    groups = ()
    org = "USAREUR-AF"
    can_query = True
    releasability = ()


@pytest.fixture(autouse=True)
def _reset_cache():
    rag_search._embedding_model_checked = False
    yield
    rag_search._embedding_model_checked = False


def _stub_claims(monkeypatch, audits: list) -> None:
    @contextmanager
    def _session():
        yield object()

    monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _Claims())
    monkeypatch.setattr(rag_search, "get_session", lambda: iter([_session()]))
    monkeypatch.setattr(rag_search, "allowed_classifications", lambda _s, _c: ["SECRET"])
    monkeypatch.setattr(
        rag_search, "_audit", lambda claims, action, detail: audits.append((action, detail))
    )


class _Point:
    def __init__(self, payload):
        self.payload = payload


class _Client:
    """Minimal stand-in for the bits collection_embedding_model() touches."""

    def __init__(self, *, exists=True, points=None, raises=False):
        self._exists, self._points, self._raises = exists, points or [], raises

    def collection_exists(self, _name):
        return self._exists

    def scroll(self, **_kwargs):
        if self._raises:
            raise RuntimeError("qdrant unreachable")
        return self._points, None


class TestCollectionEmbeddingModel:
    def test_reads_the_model_off_a_sampled_point(self, monkeypatch):
        client = _Client(points=[_Point({"embedding_model": "nomic-embed-text"})])

        assert qdrant_store.collection_embedding_model(client) == "nomic-embed-text"

    def test_absent_collection_is_unknown_not_an_error(self, monkeypatch):
        assert qdrant_store.collection_embedding_model(_Client(exists=False)) is None

    def test_empty_collection_is_unknown(self, monkeypatch):
        assert qdrant_store.collection_embedding_model(_Client(points=[])) is None

    def test_unstamped_legacy_point_is_unknown(self, monkeypatch):
        """Points written before #122 carry no provenance. That must read as
        'cannot tell', never as a mismatch."""
        client = _Client(points=[_Point({"document_id": "d1", "text": "body"})])

        assert qdrant_store.collection_embedding_model(client) is None


def _patch_store_model(monkeypatch, model_fn):
    """#160: rag_search reads provenance via the vector-store seam."""

    class _Store:
        def stored_embedding_model(self):
            return model_fn()

    monkeypatch.setattr(rag_search, "get_store", _Store)


class TestMismatchDetection:
    def test_matching_model_permits_the_query(self, monkeypatch):
        monkeypatch.setattr(rag_search, "EMBEDDING_MODEL", "nomic-embed-text")
        _patch_store_model(monkeypatch, lambda: "nomic-embed-text")

        assert rag_search._embedding_model_mismatch() is None

    def test_differing_model_is_reported(self, monkeypatch):
        monkeypatch.setattr(rag_search, "EMBEDDING_MODEL", "all-minilm")
        _patch_store_model(monkeypatch, lambda: "nomic-embed-text")

        msg = rag_search._embedding_model_mismatch()

        assert msg is not None
        assert "nomic-embed-text" in msg and "all-minilm" in msg

    def test_unknown_provenance_does_not_block(self, monkeypatch):
        """Upgrading a deployment with an existing corpus must not break it."""
        _patch_store_model(monkeypatch, lambda: None)

        assert rag_search._embedding_model_mismatch() is None

    def test_a_failing_provenance_read_never_breaks_retrieval(self, monkeypatch):
        """The check is a safety net, not a new dependency -- if Qdrant can't
        answer, retrieval proceeds and the normal error paths handle it."""

        def _boom():
            raise RuntimeError("vector store unreachable")

        _patch_store_model(monkeypatch, _boom)

        assert rag_search._embedding_model_mismatch() is None


class TestQueryRefusal:
    async def test_mismatched_query_is_refused_and_audited(self, monkeypatch):
        audits: list = []
        _stub_claims(monkeypatch, audits)
        monkeypatch.setattr(
            rag_search, "_embedding_model_mismatch", lambda: "embedding model mismatch: ..."
        )

        class _Refuse:
            def hybrid_query(self, **_kw):
                raise AssertionError("retrieval must not run on a mismatched collection")

            def access_filter_summary(self, _claims, _allowed):
                return {}

        monkeypatch.setattr(rag_search, "get_store", _Refuse)

        result = await rag_search.run_rag_search("Bearer t", "a query")

        assert result["results"] == []
        assert "mismatch" in result["error"]
        assert audits and audits[0][0] == "query.failed"
        # #125: the refusal reason is recorded, the query text is not.
        assert "a query" not in str(audits[0][1])

    async def test_matching_model_reaches_retrieval(self, monkeypatch):
        audits: list = []
        _stub_claims(monkeypatch, audits)
        monkeypatch.setattr(rag_search, "_embedding_model_mismatch", lambda: None)
        reached = False

        class _Store:
            def access_filter_summary(self, _claims, _allowed):
                return {}

            def hybrid_query(self, **_kw):
                nonlocal reached
                reached = True
                raise RuntimeError("stop here")

        monkeypatch.setattr(rag_search, "get_store", _Store)

        async def _embed(_q):
            return [0.0]

        monkeypatch.setattr(rag_search, "_embed_query", _embed)
        monkeypatch.setattr(rag_search, "embed_sparse", lambda _t: [object()])

        with pytest.raises(RuntimeError):
            await rag_search.run_rag_search("Bearer t", "a query")

        assert reached, "a matching model must not block retrieval"
