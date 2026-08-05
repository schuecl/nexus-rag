"""Coverage for issue #392: embed_texts must send nomic-embed-text's required
search_document: prefix, not the bare chunk text -- see
common/embedding_prefixes.py for why the prefix is model-gated."""

from __future__ import annotations

import httpx

from app import embedding


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


async def test_default_model_prefixes_text_as_search_document(monkeypatch):
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "nomic-embed-text")

    await embedding.embed_texts(["hello world"])

    assert captured[0]["prompt"] == "search_document: hello world"


async def test_unrecognized_model_sends_text_unprefixed(monkeypatch):
    """A configured model this repo has no prefix scheme for must not have
    one guessed at it -- today's (correct) no-prefix behavior."""
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "all-minilm")

    await embedding.embed_texts(["hello world"])

    assert captured[0]["prompt"] == "hello world"


async def test_prefix_applied_per_text_across_a_batch(monkeypatch):
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "nomic-embed-text")

    await embedding.embed_texts(["one", "two"])

    assert [c["prompt"] for c in captured] == ["search_document: one", "search_document: two"]
