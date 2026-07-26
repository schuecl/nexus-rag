"""Unit tests for the orchestration-mcp reranking pass (FR-25): result
reordering, top-k truncation, and -- the important one -- graceful fallback
to the fused order when reranker-service is down, since reranking must never
be the thing that takes retrieval offline.
"""

from __future__ import annotations

import httpx
import respx

from app.reranking import rerank

RERANK_URL = "http://reranker-service:8003/rerank"


def _candidates(*ids: str) -> list[dict]:
    return [{"id": i, "payload": {"text": f"text of {i}"}} for i in ids]


class TestRerank:
    async def test_empty_candidates_short_circuit(self):
        ranked, note = await rerank("query", [], top_k=5)
        assert ranked == []
        assert "no candidates" in note

    @respx.mock
    async def test_reorders_by_cross_encoder_score(self):
        respx.post(RERANK_URL).mock(return_value=httpx.Response(
            200, json=[{"id": "a", "score": 0.1},
                       {"id": "b", "score": 0.9},
                       {"id": "c", "score": 0.5}],
        ))
        ranked, note = await rerank("query", _candidates("a", "b", "c"), top_k=3)
        assert [c["id"] for c in ranked] == ["b", "c", "a"]
        assert "rerank" in note

    @respx.mock
    async def test_truncates_to_top_k(self):
        respx.post(RERANK_URL).mock(return_value=httpx.Response(
            200, json=[{"id": i, "score": 1.0} for i in "abcde"],
        ))
        ranked, _ = await rerank("query", _candidates("a", "b", "c", "d", "e"), top_k=2)
        assert len(ranked) == 2

    @respx.mock
    async def test_http_error_falls_back_to_fused_order(self):
        respx.post(RERANK_URL).mock(side_effect=httpx.ConnectError("down"))
        candidates = _candidates("a", "b", "c")
        ranked, note = await rerank("query", candidates, top_k=2)
        assert [c["id"] for c in ranked] == ["a", "b"]  # original order, truncated
        assert "unavailable" in note

    @respx.mock
    async def test_non_2xx_falls_back_to_fused_order(self):
        respx.post(RERANK_URL).mock(return_value=httpx.Response(503))
        candidates = _candidates("a", "b", "c")
        ranked, note = await rerank("query", candidates, top_k=3)
        assert [c["id"] for c in ranked] == ["a", "b", "c"]
        assert "unavailable" in note

    @respx.mock
    async def test_candidates_missing_from_scores_sort_last(self):
        respx.post(RERANK_URL).mock(return_value=httpx.Response(
            200, json=[{"id": "b", "score": 0.9}],
        ))
        ranked, _ = await rerank("query", _candidates("a", "b"), top_k=2)
        assert ranked[0]["id"] == "b"

    @respx.mock
    async def test_request_payload_contains_ids_and_texts(self):
        route = respx.post(RERANK_URL).mock(return_value=httpx.Response(200, json=[]))
        await rerank("query", _candidates("a"), top_k=1)
        body = route.calls.last.request.read()
        assert b'"a"' in body and b"text of a" in body
