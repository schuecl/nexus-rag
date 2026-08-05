"""Issue #216: /rerank otherwise has no authorization model of its own --
reachability on the network is authorization. These cover the shared-secret
dependency directly rather than through TestClient, since going through the
app would trigger the lifespan's real CrossEncoder load."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import main, metrics


def test_noop_when_no_secret_configured(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "")

    main._check_shared_secret(x_reranker_shared_secret=None)
    main._check_shared_secret(x_reranker_shared_secret="anything")


def test_accepts_matching_header(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    main._check_shared_secret(x_reranker_shared_secret="s3cr3t")


def test_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    with pytest.raises(HTTPException) as exc_info:
        main._check_shared_secret(x_reranker_shared_secret=None)
    assert exc_info.value.status_code == 401


def test_rejects_wrong_header(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    with pytest.raises(HTTPException) as exc_info:
        main._check_shared_secret(x_reranker_shared_secret="wrong")
    assert exc_info.value.status_code == 401


def test_rejects_empty_header_even_if_secret_is_falsy_like(monkeypatch):
    # Guards against a future refactor reintroducing `if not secret: return`
    # ambiguity between "unconfigured" and "configured as an empty string".
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "s3cr3t")

    with pytest.raises(HTTPException):
        main._check_shared_secret(x_reranker_shared_secret="")


def test_accepts_previous_secret_during_rotation(monkeypatch):
    # Issue #281 gap G5 stage 2: orchestration-mcp restarted with the new
    # secret before this service has -- the old value must still work until
    # this service is restarted too (docs/credential-rotation.md).
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "new-s3cr3t")
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET_PREVIOUS", "old-s3cr3t")

    main._check_shared_secret(x_reranker_shared_secret="old-s3cr3t")
    main._check_shared_secret(x_reranker_shared_secret="new-s3cr3t")


def test_rejects_stale_secret_once_previous_unset(monkeypatch):
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET", "new-s3cr3t")
    monkeypatch.setattr(main, "RERANKER_SHARED_SECRET_PREVIOUS", "")

    with pytest.raises(HTTPException) as exc_info:
        main._check_shared_secret(x_reranker_shared_secret="old-s3cr3t")
    assert exc_info.value.status_code == 401


class _FakeTokenizer:
    """One token per whitespace word; decode joins them back. Deterministic
    stand-in so windowing is testable without the real model."""

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": text.split()}

    def decode(self, ids):
        return " ".join(ids)


class TestWindowTexts:
    def test_fitting_pair_is_returned_unchanged(self):
        texts, oversized = main._window_texts(_FakeTokenizer(), "q w", "a b c", max_length=10)

        assert texts == ["a b c"]
        assert oversized is False

    def test_oversized_chunk_is_split_into_overlapping_windows(self):
        # budget = 10 - 1 (query) - 3 (specials) = 6; 12 words -> windows.
        chunk = " ".join(f"w{i}" for i in range(12))

        texts, oversized = main._window_texts(_FakeTokenizer(), "q", chunk, max_length=10)

        assert oversized is True
        assert len(texts) > 1
        # Every word appears in at least one window -- nothing silently lost.
        seen = set()
        for t in texts:
            seen.update(t.split())
        assert seen == {f"w{i}" for i in range(12)}
        # Consecutive windows overlap so a boundary-straddling passage is
        # seen whole by one of them.
        assert set(texts[0].split()) & set(texts[1].split())

    def test_query_overfilling_the_window_degrades_to_the_raw_text_but_still_counts(self):
        # Windowing the chunk can't help when the query alone exhausts the
        # budget, but the pair is still oversized -- the caller's metric
        # must count it rather than silently missing every over-length
        # query (the case a near-MAX_QUERY_CHARS request hits in practice).
        long_query = " ".join(f"q{i}" for i in range(20))

        texts, oversized = main._window_texts(_FakeTokenizer(), long_query, "a b c", max_length=10)

        assert texts == ["a b c"]
        assert oversized is True

    def test_query_exactly_filling_budget_with_empty_text_is_not_oversized(self):
        query = " ".join(f"q{i}" for i in range(7))  # budget = 10 - 7 - 3 = 0

        texts, oversized = main._window_texts(_FakeTokenizer(), query, "", max_length=10)

        assert texts == [""]
        assert oversized is False


class _FakeModel:
    """Scores a pair by the number of 'hit' tokens in its text -- so a chunk
    whose relevant passage sits past the window boundary only scores when
    windowing lets the model see it."""

    tokenizer = _FakeTokenizer()

    def predict(self, pairs):
        return [float(text.split().count("hit")) for _query, text in pairs]


def _rerank_body(chunks: dict[str, str]) -> main.RerankRequest:
    return main.RerankRequest(
        query="q",
        chunks=[main.Chunk(id=id_, text=text) for id_, text in chunks.items()],
    )


def test_rerank_scores_oversized_chunk_by_its_best_window(monkeypatch):
    # #393: 'tail' hides its relevant passage beyond the first window; with
    # max-over-windows it must outrank 'head', whose window-sized text has
    # nothing relevant.
    monkeypatch.setattr(main, "_model", _FakeModel())
    monkeypatch.setattr(main, "MAX_LENGTH", 10)
    monkeypatch.setattr(main, "WINDOW_SCORING", True)
    filler = " ".join(f"w{i}" for i in range(8))
    body = _rerank_body({"tail": f"{filler} hit hit hit", "head": "nothing relevant here"})

    ranked = main.rerank(body)

    assert [r.id for r in ranked] == ["tail", "head"]
    assert ranked[0].score == 3.0


def test_rerank_window_scoring_off_restores_head_only_scoring(monkeypatch):
    monkeypatch.setattr(main, "_model", _FakeModel())
    monkeypatch.setattr(main, "MAX_LENGTH", 10)
    monkeypatch.setattr(main, "WINDOW_SCORING", False)
    filler = " ".join(f"w{i}" for i in range(8))
    # _FakeModel sees the whole text either way (it doesn't truncate), so the
    # assertion here is structural: one pair per chunk, no window expansion.
    body = _rerank_body({"tail": f"{filler} hit", "head": "x"})

    ranked = main.rerank(body)

    assert {r.id for r in ranked} == {"tail", "head"}


def test_rerank_counts_oversized_chunk_even_when_query_alone_fills_the_window(monkeypatch):
    # A near-MAX_QUERY_CHARS request (orchestration-mcp allows up to 4000
    # chars) can tokenize past the window by itself, leaving no budget for
    # windowing. That pair still gets truncated by the model, so it must
    # still increment the metric -- the earlier bug returned len(texts) == 1
    # for this case and the caller mistook that for "not oversized".
    monkeypatch.setattr(main, "_model", _FakeModel())
    monkeypatch.setattr(main, "MAX_LENGTH", 10)
    monkeypatch.setattr(main, "WINDOW_SCORING", True)
    long_query = " ".join(f"q{i}" for i in range(20))
    body = main.RerankRequest(query=long_query, chunks=[main.Chunk(id="a", text="a b c")])
    before = metrics.oversized_chunks_total.labels(handling="windowed")._value.get()

    main.rerank(body)

    after = metrics.oversized_chunks_total.labels(handling="windowed")._value.get()
    assert after == before + 1


def test_rerank_fitting_chunks_are_scored_exactly_once(monkeypatch):
    calls: list[int] = []

    class _CountingModel(_FakeModel):
        def predict(self, pairs):
            calls.append(len(pairs))
            return super().predict(pairs)

    monkeypatch.setattr(main, "_model", _CountingModel())
    monkeypatch.setattr(main, "MAX_LENGTH", 50)
    monkeypatch.setattr(main, "WINDOW_SCORING", True)
    body = _rerank_body({"a": "hit one", "b": "two"})

    ranked = main.rerank(body)

    assert calls == [2]  # one pair per chunk, no windows needed
    assert [r.id for r in ranked] == ["a", "b"]
