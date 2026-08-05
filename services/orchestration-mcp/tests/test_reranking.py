"""Coverage for issue #89: rerank() applies an optional per-content-type
score multiplier to the cross-encoder's scores before sorting, so a chunk's
content_type (tagged by ingestion-worker, see services/ingestion-worker/app/
chunking.py) can shift its rank without changing the reranker-service
contract itself."""

from __future__ import annotations

import httpx
import pytest

from app import reranking


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _mock_reranker(monkeypatch, scores_by_id: dict[str, float]):
    async def fake_post(self, url, json=None, **kwargs):
        return _FakeResponse(
            [{"id": chunk["id"], "score": scores_by_id[chunk["id"]]} for chunk in json["chunks"]]
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


def _candidate(id_: str, content_type: str) -> dict:
    return {"id": id_, "payload": {"text": id_, "content_type": content_type}}


async def test_no_boost_keeps_cross_encoder_order(monkeypatch):
    candidates = [_candidate("a", "text"), _candidate("b", "table")]
    _mock_reranker(monkeypatch, {"a": 0.9, "b": 0.5})

    ranked, note = await reranking.rerank("q", candidates, top_k=2)

    assert [c["id"] for c in ranked] == ["a", "b"]
    assert "content-type boosts" not in note


async def test_content_type_boost_can_flip_the_ranking(monkeypatch):
    candidates = [_candidate("a", "text"), _candidate("b", "table")]
    _mock_reranker(monkeypatch, {"a": 0.9, "b": 0.5})

    ranked, note = await reranking.rerank(
        "q", candidates, top_k=2, content_type_boosts={"table": 2.0}
    )

    assert [c["id"] for c in ranked] == ["b", "a"]
    assert "content-type boosts" in note


async def test_missing_content_type_defaults_to_text_weight(monkeypatch):
    candidate = {"id": "a", "payload": {"text": "a"}}  # no content_type key
    _mock_reranker(monkeypatch, {"a": 0.7})

    ranked, _ = await reranking.rerank(
        "q", [candidate], top_k=1, content_type_boosts={"table": 5.0}
    )

    assert [c["id"] for c in ranked] == ["a"]


async def test_reranker_outage_falls_back_to_fused_order_regardless_of_boosts(monkeypatch):
    async def fake_post(self, url, json=None, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    candidates = [_candidate("a", "text"), _candidate("b", "table")]
    ranked, note = await reranking.rerank(
        "q", candidates, top_k=2, content_type_boosts={"table": 2.0}
    )

    assert ranked == candidates[:2]
    assert "unavailable" in note


async def test_empty_candidates_short_circuits():
    ranked, note = await reranking.rerank("q", [], top_k=5)
    assert ranked == []
    assert note == "no candidates to rerank"


def test_load_content_type_boosts_from_env(monkeypatch):
    monkeypatch.setenv("CONTENT_TYPE_BOOSTS", '{"table": 1.5}')
    assert reranking._load_content_type_boosts() == {"table": 1.5}


def test_load_content_type_boosts_ignores_malformed_env(monkeypatch):
    monkeypatch.setenv("CONTENT_TYPE_BOOSTS", "not json")
    assert reranking._load_content_type_boosts() == {}


def test_load_content_type_boosts_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("CONTENT_TYPE_BOOSTS", raising=False)
    assert reranking._load_content_type_boosts() == {}


async def test_sends_shared_secret_header_when_configured(monkeypatch):
    monkeypatch.setattr(reranking, "RERANKER_SHARED_SECRET", "s3cr3t")
    seen_headers = {}

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        seen_headers.update(headers or {})
        return _FakeResponse([{"id": "a", "score": 0.5}])

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await reranking.rerank("q", [_candidate("a", "text")], top_k=1)

    assert seen_headers == {"X-Reranker-Shared-Secret": "s3cr3t"}


async def test_omits_shared_secret_header_when_unconfigured(monkeypatch):
    monkeypatch.setattr(reranking, "RERANKER_SHARED_SECRET", "")
    seen_headers = {}

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        seen_headers.update(headers or {})
        return _FakeResponse([{"id": "a", "score": 0.5}])

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await reranking.rerank("q", [_candidate("a", "text")], top_k=1)

    assert seen_headers == {}


class TestTeiCompatibility:
    """Issue #419: RERANKER_API_COMPATIBILITY="tei" targets HuggingFace
    text-embeddings-inference's native /rerank shape -- {query, texts} ->
    [{index, score}], index-addressed rather than id-addressed like the
    internal reranker-service shape."""

    async def test_reorders_by_index_addressed_score(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "tei")
        candidates = [_candidate("a", "text"), _candidate("b", "text"), _candidate("c", "text")]
        seen_body = {}

        async def fake_post(self, url, json=None, headers=None, **kwargs):
            seen_body.update(json)
            assert url == "http://reranker-service:8003/rerank"
            return _FakeResponse([{"index": 0, "score": 0.1}, {"index": 1, "score": 0.9}])

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        ranked, note = await reranking.rerank("q", candidates, top_k=3)

        assert seen_body == {"query": "q", "texts": ["a", "b", "c"]}
        assert [c["id"] for c in ranked] == ["b", "a", "c"]
        assert "reranking applied" in note

    async def test_sends_bearer_token_when_configured(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "tei")
        monkeypatch.setattr(reranking, "RERANKER_API_KEY", "tei-key")
        seen_headers = {}

        async def fake_post(self, url, json=None, headers=None, **kwargs):
            seen_headers.update(headers or {})
            return _FakeResponse([{"index": 0, "score": 0.5}])

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        await reranking.rerank("q", [_candidate("a", "text")], top_k=1)

        assert seen_headers == {"Authorization": "Bearer tei-key"}

    async def test_omits_shared_secret_header_even_when_configured(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "tei")
        monkeypatch.setattr(reranking, "RERANKER_SHARED_SECRET", "internal-secret")
        seen_headers = {}

        async def fake_post(self, url, json=None, headers=None, **kwargs):
            seen_headers.update(headers or {})
            return _FakeResponse([{"index": 0, "score": 0.5}])

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        await reranking.rerank("q", [_candidate("a", "text")], top_k=1)

        assert seen_headers == {}

    async def test_falls_back_to_fused_order_on_outage(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "tei")

        async def fake_post(self, url, json=None, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        candidates = [_candidate("a", "text"), _candidate("b", "text")]
        ranked, note = await reranking.rerank("q", candidates, top_k=2)

        assert ranked == candidates
        assert "unavailable" in note


class TestCohereCompatibility:
    """Issue #419: RERANKER_API_COMPATIBILITY="cohere" targets the Jina/
    Cohere-style /v1/rerank convention -- also what vLLM's own rerank
    endpoints speak, unlike "tei" above."""

    async def test_reorders_by_index_addressed_relevance_score(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "cohere")
        monkeypatch.setattr(reranking, "RERANKER_MODEL", "BAAI/bge-reranker-base")
        candidates = [_candidate("a", "text"), _candidate("b", "text")]
        seen_body = {}

        async def fake_post(self, url, json=None, headers=None, **kwargs):
            seen_body.update(json)
            assert url == "http://reranker-service:8003/v1/rerank"
            return _FakeResponse(
                {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                }
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        ranked, _note = await reranking.rerank("q", candidates, top_k=2)

        assert seen_body == {
            "model": "BAAI/bge-reranker-base",
            "query": "q",
            "documents": ["a", "b"],
            "top_n": 2,
        }
        assert [c["id"] for c in ranked] == ["b", "a"]

    async def test_sends_bearer_token_when_configured(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "cohere")
        monkeypatch.setattr(reranking, "RERANKER_API_KEY", "cohere-key")
        seen_headers = {}

        async def fake_post(self, url, json=None, headers=None, **kwargs):
            seen_headers.update(headers or {})
            return _FakeResponse({"results": [{"index": 0, "relevance_score": 0.5}]})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        await reranking.rerank("q", [_candidate("a", "text")], top_k=1)

        assert seen_headers == {"Authorization": "Bearer cohere-key"}

    async def test_falls_back_to_fused_order_on_non_2xx(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANKER_API_COMPATIBILITY", "cohere")

        async def fake_post(self, url, json=None, **kwargs):
            raise httpx.HTTPStatusError("503", request=None, response=httpx.Response(503))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        candidates = [_candidate("a", "text"), _candidate("b", "text")]
        ranked, note = await reranking.rerank("q", candidates, top_k=2)

        assert ranked == candidates
        assert "unavailable" in note


def _chunk(id_: str, doc: str, idx, score_rank: int = 0) -> dict:
    """A candidate carrying the payload fields the #395 collapse keys on."""
    return {
        "id": id_,
        "payload": {"text": id_, "content_type": "text", "document_id": doc, "chunk_index": idx},
    }


class TestCollapseAdjacentOverlaps:
    """Issue #395: same-document adjacent-index chunks share text by
    construction (FR-4 overlap), so the better-ranked one keeps the slot and
    the freed slot backfills from the remaining pool."""

    def test_adjacent_pair_collapses_to_the_better_ranked_and_backfills(self):
        ranked = [
            _chunk("a", "doc1", 4),
            _chunk("b", "doc1", 5),  # adjacent to kept "a" -- dropped
            _chunk("c", "doc2", 0),
            _chunk("d", "doc3", 7),
        ]

        kept, dropped = reranking.collapse_adjacent_overlaps(ranked, top_k=3)

        assert [c["id"] for c in kept] == ["a", "c", "d"]
        assert dropped == 1

    def test_non_adjacent_same_document_chunks_both_survive(self):
        ranked = [_chunk("a", "doc1", 2), _chunk("b", "doc1", 9)]

        kept, dropped = reranking.collapse_adjacent_overlaps(ranked, top_k=2)

        assert [c["id"] for c in kept] == ["a", "b"]
        assert dropped == 0

    def test_same_index_different_documents_both_survive(self):
        ranked = [_chunk("a", "doc1", 3), _chunk("b", "doc2", 4)]

        kept, dropped = reranking.collapse_adjacent_overlaps(ranked, top_k=2)

        assert [c["id"] for c in kept] == ["a", "b"]
        assert dropped == 0

    def test_chain_collapses_both_neighbours_of_a_kept_chunk(self):
        # Ranked i, i+1, i-1: both neighbours overlap the kept middle chunk.
        ranked = [
            _chunk("mid", "doc1", 5),
            _chunk("next", "doc1", 6),
            _chunk("prev", "doc1", 4),
            _chunk("other", "doc2", 0),
        ]

        kept, dropped = reranking.collapse_adjacent_overlaps(ranked, top_k=3)

        assert [c["id"] for c in kept] == ["mid", "other"]
        assert dropped == 2

    def test_missing_identity_fields_are_never_collapsed(self):
        no_doc = {"id": "x", "payload": {"text": "x", "chunk_index": 5}}
        no_idx = {"id": "y", "payload": {"text": "y", "document_id": "doc1"}}
        ranked = [_chunk("a", "doc1", 5), no_doc, no_idx]

        kept, dropped = reranking.collapse_adjacent_overlaps(ranked, top_k=3)

        assert [c["id"] for c in kept] == ["a", "x", "y"]
        assert dropped == 0

    def test_truncates_to_top_k_after_collapsing(self):
        ranked = [_chunk(str(i), f"doc{i}", 0) for i in range(6)]

        kept, dropped = reranking.collapse_adjacent_overlaps(ranked, top_k=4)

        assert len(kept) == 4
        assert dropped == 0


async def test_rerank_collapses_adjacent_pair_and_notes_it(monkeypatch):
    candidates = [
        _chunk("a", "doc1", 4),
        _chunk("b", "doc1", 5),
        _chunk("c", "doc2", 0),
    ]
    _mock_reranker(monkeypatch, {"a": 0.9, "b": 0.8, "c": 0.5})

    reranked, note = await reranking.rerank("q", candidates, top_k=2)

    assert [c["id"] for c in reranked] == ["a", "c"]
    assert "1 overlap-adjacent duplicate(s) collapsed" in note


async def test_rerank_keeps_the_higher_scoring_side_of_the_pair(monkeypatch):
    # The lower-indexed chunk is NOT automatically the survivor -- rank is.
    candidates = [
        _chunk("a", "doc1", 4),
        _chunk("b", "doc1", 5),
        _chunk("c", "doc2", 0),
    ]
    _mock_reranker(monkeypatch, {"a": 0.2, "b": 0.9, "c": 0.5})

    reranked, _note = await reranking.rerank("q", candidates, top_k=2)

    assert [c["id"] for c in reranked] == ["b", "c"]


async def test_rerank_without_duplicates_does_not_mention_collapsing(monkeypatch):
    candidates = [_chunk("a", "doc1", 4), _chunk("c", "doc2", 0)]
    _mock_reranker(monkeypatch, {"a": 0.9, "c": 0.5})

    _, note = await reranking.rerank("q", candidates, top_k=2)

    assert "collapsed" not in note


async def test_degraded_path_also_collapses(monkeypatch):
    async def fail_post(self, url, json=None, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)
    candidates = [
        _chunk("a", "doc1", 4),
        _chunk("b", "doc1", 5),
        _chunk("c", "doc2", 0),
    ]

    reranked, note = await reranking.rerank("q", candidates, top_k=2)

    assert [c["id"] for c in reranked] == ["a", "c"]
    assert "reranking unavailable" in note
    assert "1 overlap-adjacent duplicate(s) collapsed" in note


class TestRelevanceFloor:
    """Issue #394: candidates below RERANK_SCORE_FLOOR are dropped, and a
    query where everything drops returns [] so rag_search's existing
    no-results guidance fires instead of the least-bad top_k."""

    def test_load_floor_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("RERANK_SCORE_FLOOR", raising=False)

        assert reranking._load_score_floor() is None

    def test_load_floor_parses_a_number(self, monkeypatch):
        monkeypatch.setenv("RERANK_SCORE_FLOOR", "-5.0")

        assert reranking._load_score_floor() == -5.0

    def test_load_floor_bad_value_disables_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("RERANK_SCORE_FLOOR", "very strict")

        with caplog.at_level("WARNING"):
            assert reranking._load_score_floor() is None

        assert "relevance floor disabled" in caplog.text

    async def test_below_floor_candidates_are_dropped_and_noted(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", -5.0)
        candidates = [_candidate("good", "text"), _candidate("bad", "text")]
        _mock_reranker(monkeypatch, {"good": 2.0, "bad": -9.0})

        reranked, note = await reranking.rerank("q", candidates, top_k=5)

        assert [c["id"] for c in reranked] == ["good"]
        assert "1 candidate(s) below the relevance floor (-5.0)" in note

    async def test_all_below_floor_returns_empty(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", -5.0)
        candidates = [_candidate("a", "text"), _candidate("b", "text")]
        _mock_reranker(monkeypatch, {"a": -9.0, "b": -11.0})

        reranked, note = await reranking.rerank("q", candidates, top_k=5)

        assert reranked == []
        assert "2 candidate(s) below the relevance floor" in note

    async def test_floor_off_keeps_todays_behavior(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", None)
        candidates = [_candidate("a", "text"), _candidate("b", "text")]
        _mock_reranker(monkeypatch, {"a": -9.0, "b": -11.0})

        reranked, note = await reranking.rerank("q", candidates, top_k=5)

        assert [c["id"] for c in reranked] == ["a", "b"]
        assert "floor" not in note

    async def test_score_exactly_at_the_floor_survives(self, monkeypatch):
        monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", -5.0)
        candidates = [_candidate("edge", "text")]
        _mock_reranker(monkeypatch, {"edge": -5.0})

        reranked, _note = await reranking.rerank("q", candidates, top_k=5)

        assert [c["id"] for c in reranked] == ["edge"]

    async def test_floor_applies_to_the_boosted_score(self, monkeypatch):
        # A boost can lift a borderline candidate over the floor -- the floor
        # judges what the ranking actually sorted on, not the raw score.
        monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", -5.0)
        candidates = [_candidate("t", "table")]
        _mock_reranker(monkeypatch, {"t": -6.0})

        reranked, _note = await reranking.rerank(
            "q", candidates, top_k=5, content_type_boosts={"table": 0.5}
        )

        assert [c["id"] for c in reranked] == ["t"]  # -6.0 * 0.5 = -3.0 >= -5.0

    async def test_degraded_path_has_no_scores_so_no_floor(self, monkeypatch):
        async def fail_post(self, url, json=None, **kwargs):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)
        monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", -5.0)
        candidates = [_candidate("a", "text")]

        reranked, note = await reranking.rerank("q", candidates, top_k=5)

        assert [c["id"] for c in reranked] == ["a"]
        assert "unavailable" in note


async def test_floor_filters_before_collapse_and_backfill_respects_it(monkeypatch):
    """#394 + #395 interaction: the floor judges relevance first, then the
    collapse dedups what remains -- so a below-floor chunk is reported as
    below-floor (not as a collapsed duplicate), and the collapse's backfill
    never resurrects a candidate the floor rejected."""
    monkeypatch.setattr(reranking, "RERANK_SCORE_FLOOR", -5.0)
    candidates = [
        _chunk("a", "doc1", 4),  # relevant, kept
        _chunk("b", "doc1", 5),  # adjacent to a, above floor -> collapsed
        _chunk("c", "doc2", 0),  # below floor -> dropped by the floor
        _chunk("d", "doc3", 9),  # above floor -> backfills
    ]
    _mock_reranker(monkeypatch, {"a": 3.0, "b": 2.0, "c": -9.0, "d": 0.5})

    reranked, note = await reranking.rerank("q", candidates, top_k=3)

    assert [c["id"] for c in reranked] == ["a", "d"]
    assert "1 candidate(s) below the relevance floor (-5.0)" in note
    assert "1 overlap-adjacent duplicate(s) collapsed" in note


class TestFloorParseRejectsNonFinite:
    """Review on #415: float() parses "inf"/"nan" happily. inf empties every
    query -- a retrieval outage dressed as configuration -- and nan poisons
    every comparison. Both must trip the warn-and-disable fallback."""

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "NaN", "Infinity"])
    def test_non_finite_values_disable_the_floor_with_a_warning(self, monkeypatch, caplog, raw):
        monkeypatch.setenv("RERANK_SCORE_FLOOR", raw)

        with caplog.at_level("WARNING"):
            assert reranking._load_score_floor() is None

        assert "not a finite number" in caplog.text
