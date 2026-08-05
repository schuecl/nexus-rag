"""Coverage for issue #392 (embed_texts must send nomic-embed-text's required
search_document: prefix, not the bare chunk text -- see
common/embedding_prefixes.py for why the prefix is model-gated) and issue #396
(bounded batches through /api/embed instead of one request per chunk, with
input order preserved across batch boundaries)."""

from __future__ import annotations

import httpx
import pytest

from app import embedding


class _FakeResponse:
    def __init__(self, vectors):
        self._vectors = vectors
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"embeddings": self._vectors}


def _mock_ollama(monkeypatch, captured: list, vector_for=lambda text: [0.1, 0.2]):
    """Capture each /api/embed payload and answer with one vector per input,
    derived from the input text so order-sensitivity is observable."""

    async def fake_post(self, url, json=None, **kwargs):
        captured.append(json)
        return _FakeResponse([vector_for(text) for text in json["input"]])

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def test_default_model_prefixes_text_as_search_document(monkeypatch):
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "nomic-embed-text")

    await embedding.embed_texts(["hello world"])

    assert captured[0]["input"] == ["search_document: hello world"]


async def test_unrecognized_model_sends_text_unprefixed(monkeypatch):
    """A configured model this repo has no prefix scheme for must not have
    one guessed at it -- today's (correct) no-prefix behavior."""
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "all-minilm")

    await embedding.embed_texts(["hello world"])

    assert captured[0]["input"] == ["hello world"]


async def test_prefix_applied_per_text_across_a_batch(monkeypatch):
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "nomic-embed-text")

    await embedding.embed_texts(["one", "two"])

    assert captured[0]["input"] == ["search_document: one", "search_document: two"]


async def test_texts_are_split_into_bounded_batches(monkeypatch):
    """#396: one request per batch, never one unbounded request -- an
    unbounded batch just moves the memory problem into the embedding
    server."""
    captured: list = []
    _mock_ollama(monkeypatch, captured)
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "all-minilm")
    monkeypatch.setattr(embedding, "EMBEDDING_BATCH_SIZE", 3)

    await embedding.embed_texts([f"text-{i}" for i in range(7)])

    assert [len(c["input"]) for c in captured] == [3, 3, 1]


async def test_vector_order_matches_text_order_across_batches(monkeypatch):
    """The caller stores vectors[i] as the embedding of texts[i]; batch
    boundaries must not be able to reorder that mapping."""
    captured: list = []
    _mock_ollama(monkeypatch, captured, vector_for=lambda text: [float(text.split("-")[1])])
    monkeypatch.setattr(embedding, "EMBEDDING_MODEL", "all-minilm")
    monkeypatch.setattr(embedding, "EMBEDDING_BATCH_SIZE", 2)

    vectors = await embedding.embed_texts([f"text-{i}" for i in range(5)])

    assert vectors == [[0.0], [1.0], [2.0], [3.0], [4.0]]


async def test_http_error_becomes_embedding_error(monkeypatch):
    async def fail_post(self, url, json=None, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)

    with pytest.raises(embedding.EmbeddingError):
        await embedding.embed_texts(["hello"])


async def test_count_mismatch_becomes_embedding_error(monkeypatch):
    """request_embeddings' misalignment guard must surface as the worker's
    permanent-failure exception, not crash the consumer as a ValueError."""

    async def short_post(self, url, json=None, **kwargs):
        return _FakeResponse([[0.1]])  # one vector for two inputs

    monkeypatch.setattr(httpx.AsyncClient, "post", short_post)

    with pytest.raises(embedding.EmbeddingError):
        await embedding.embed_texts(["one", "two"])


class TestBatchSizeParse:
    """EMBEDDING_BATCH_SIZE is read at worker import, so a bad value degrades
    loudly instead of raising -- same reasoning as #389's pool-recycle parse."""

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_BATCH_SIZE", raising=False)

        assert embedding._batch_size() == embedding.DEFAULT_BATCH_SIZE

    @pytest.mark.parametrize("raw", ["", "  "])
    def test_empty_uses_default_silently(self, monkeypatch, raw):
        monkeypatch.setenv("EMBEDDING_BATCH_SIZE", raw)

        assert embedding._batch_size() == embedding.DEFAULT_BATCH_SIZE

    def test_valid_override_honoured(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "8")

        assert embedding._batch_size() == 8

    @pytest.mark.parametrize("raw", ["surely", "0", "-4"])
    def test_bad_value_falls_back_with_warning(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("EMBEDDING_BATCH_SIZE", raw)

        with caplog.at_level("WARNING"):
            assert embedding._batch_size() == embedding.DEFAULT_BATCH_SIZE

        assert caplog.records, "a rejected value must be logged"
