"""Coverage for issue #392: _embed_query must send nomic-embed-text's
required search_query: prefix, not the bare query text -- the query-side
counterpart to ingestion-worker's embed_texts (see
services/ingestion-worker/tests/test_embedding.py and
common/embedding_prefixes.py for why the prefix is model-gated)."""

from __future__ import annotations

import httpx

from app import rag_search


class _FakeResponse:
    def __init__(self, vector):
        self._vector = vector

    def raise_for_status(self):
        pass

    def json(self):
        return {"embedding": self._vector}


def _mock_ollama(monkeypatch, captured: list):
    async def fake_post(self, url, json=None, **kwargs):
        captured.append(json)
        return _FakeResponse([0.1, 0.2])

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def test_default_model_prefixes_query_as_search_query(monkeypatch):
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(rag_search, "EMBEDDING_MODEL", "nomic-embed-text")

    await rag_search._embed_query("what is the policy")

    assert captured[0]["prompt"] == "search_query: what is the policy"


async def test_unrecognized_model_sends_query_unprefixed(monkeypatch):
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(rag_search, "EMBEDDING_MODEL", "all-minilm")

    await rag_search._embed_query("what is the policy")

    assert captured[0]["prompt"] == "what is the policy"
