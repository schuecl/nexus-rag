"""Coverage for issue #89: rerank() applies an optional per-content-type
score multiplier to the cross-encoder's scores before sorting, so a chunk's
content_type (tagged by ingestion-worker, see services/ingestion-worker/app/
chunking.py) can shift its rank without changing the reranker-service
contract itself."""

from __future__ import annotations

import httpx

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
