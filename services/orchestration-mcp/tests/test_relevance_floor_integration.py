"""Review follow-up on the #394 floor (PR #415): a floor-emptied query is a
first-class empty outcome at the rag_search level -- outcome="empty" in
queries_total and an FR-31 audit row that says why -- not a fall-through
success that happens to carry zero results. The reranking.py suite can't see
any of this integration wiring, which is how the first cut got through green.
"""

from __future__ import annotations

from contextlib import contextmanager

from qdrant_client.models import SparseVector

from app import metrics, rag_search
from app.rag_search import format_rag_search_for_model


class _Claims:
    sub = "u1"
    preferred_username = "bob-query"
    clearance = "SECRET"
    groups = ("analysts",)
    org = "USAREUR-AF"
    can_query = True
    releasability = ("FVEY",)


class _Hit:
    def __init__(self, id_, score, payload):
        self.id = id_
        self.score = score
        self.payload = payload


def _hit(id_="c1"):
    return _Hit(id_, 0.87, {"document_id": "d1", "text": "body", "content_type": "text"})


def _stub(monkeypatch, *, hits, audits: list) -> None:
    @contextmanager
    def _session():
        yield object()

    class _Store:
        def hybrid_query(self, **_kwargs):
            return hits

        def access_filter_summary(self, _claims, _allowed):
            return {"backend": "fake"}

        def stored_embedding_model(self):
            return None

    async def _embed(_q):
        return [0.0]

    monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _Claims())
    monkeypatch.setattr(rag_search, "get_session", lambda: iter([_session()]))
    monkeypatch.setattr(rag_search, "allowed_classifications", lambda _s, _c: ["SECRET"])
    monkeypatch.setattr(rag_search, "_embed_query", _embed)
    monkeypatch.setattr(
        rag_search, "embed_sparse", lambda _t: [SparseVector(indices=[0], values=[1.0])]
    )
    monkeypatch.setattr(rag_search, "get_store", _Store)
    monkeypatch.setattr(
        rag_search, "_audit", lambda claims, action, detail: audits.append((action, detail))
    )


def _empty_outcome_count() -> float:
    return metrics.queries_total.labels(outcome="empty")._value.get()


def _ok_outcome_count() -> float:
    return metrics.queries_total.labels(outcome="ok")._value.get()


async def test_floor_emptied_query_is_an_empty_outcome_with_a_reasoned_audit(monkeypatch):
    audits: list = []
    _stub(monkeypatch, hits=[_hit()], audits=audits)

    async def _floor_empties(_q, _candidates, _top_k, **_kw):
        return (
            [],
            "cross-encoder reranking applied; 1 candidate(s) below the relevance floor (-5.0)",
        )

    monkeypatch.setattr(rag_search, "rerank", _floor_empties)
    before = _empty_outcome_count()
    before_ok = _ok_outcome_count()

    result = await rag_search.run_rag_search("Bearer t", "unanswerable question")

    assert result["results"] == []
    assert "below the configured relevance floor" in result["note"]
    # Metrics: dashboards watching queries_total{outcome="empty"} must see
    # this, not an ordinary "ok" with zero results.
    assert _empty_outcome_count() == before + 1
    # The exact bug the review caught: the first cut fell through to the
    # success path, so a floor-emptied query landed in outcome="ok" and was
    # indistinguishable from a successful query that returned results.
    assert _ok_outcome_count() == before_ok
    # FR-31: the durable record explains *why* zero results came back, same
    # as the two pre-existing zero-result branches.
    action, detail = audits[0]
    assert action == "query"
    assert detail["result_count"] == 0
    assert "below the configured relevance floor" in detail["note"]


async def test_the_floor_reaches_the_model_as_an_abstention_instruction(monkeypatch):
    """The whole point of #394, end to end: the floor's decision has to arrive
    at the generation model as "say no document covers this", not as silence.
    Chains the real formatter onto the real search path so the claim is pinned
    at the layer the model actually reads, not just in the result dict.
    """
    _stub(monkeypatch, hits=[_hit()], audits=[])

    async def _floor_empties(_q, _candidates, _top_k, **_kw):
        return ([], "cross-encoder reranking applied; 1 candidate(s) below the relevance floor")

    monkeypatch.setattr(rag_search, "rerank", _floor_empties)

    rendered = format_rag_search_for_model(
        await rag_search.run_rag_search("Bearer t", "unanswerable question")
    )

    assert "no approved document" in rendered
    # And the reason rides along, so an operator reading a transcript can tell
    # a relevance-floor abstention from an empty corpus or an access denial.
    assert "below the configured relevance floor" in rendered


async def test_surviving_results_keep_the_ordinary_success_path(monkeypatch):
    audits: list = []
    _stub(monkeypatch, hits=[_hit()], audits=audits)

    async def _rerank(_q, candidates, top_k, **_kw):
        return candidates[:top_k], "cross-encoder reranking applied"

    monkeypatch.setattr(rag_search, "rerank", _rerank)
    before = _empty_outcome_count()

    result = await rag_search.run_rag_search("Bearer t", "answerable question")

    assert len(result["results"]) == 1
    assert "note" not in result
    assert _empty_outcome_count() == before
    _, detail = audits[0]
    assert detail["result_count"] == 1
