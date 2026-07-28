"""Coverage for issue #72: the retrieval path is measurable.

Two surfaces with deliberately different contents -- per-request timings in the
FR-31 audit entry, aggregates on /metrics -- and one property they share:
neither may leak query content or per-user identity into a place that is more
widely readable than the corpus (#125, #127).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from qdrant_client.models import SparseVector
from starlette.requests import Request

from app import metrics, rag_search, server


class _Claims:
    sub = "u1"
    preferred_username = "bob-query"
    clearance = "SECRET"
    groups = ()
    org = "USAREUR-AF"
    can_query = True
    releasability = ()


class _Hit:
    def __init__(self, id_="c1"):
        self.id = id_
        self.score = 0.9
        self.payload = {"document_id": "d1", "text": "body", "content_type": "text"}


def _stub(monkeypatch, audits: list, *, hits=None, rerank_note="cross-encoder rerank"):
    @contextmanager
    def _session():
        yield object()

    class _Store:  # #160: rag_search goes through the vector-store seam now
        def hybrid_query(self, **_kw):
            return hits if hits is not None else [_Hit()]

        def access_filter_summary(self, _claims, _allowed):
            return {"backend": "fake"}

        def stored_embedding_model(self):
            return None

    async def _embed(_q):
        return [0.0]

    async def _rerank(_q, candidates, top_k, **_kw):
        return candidates[:top_k], rerank_note

    monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _Claims())
    monkeypatch.setattr(rag_search, "get_session", lambda: iter([_session()]))
    monkeypatch.setattr(rag_search, "allowed_classifications", lambda _s, _c: ["SECRET"])
    monkeypatch.setattr(rag_search, "_embed_query", _embed)
    monkeypatch.setattr(
        rag_search, "embed_sparse", lambda _t: [SparseVector(indices=[0], values=[1.0])]
    )
    monkeypatch.setattr(rag_search, "get_store", _Store)
    monkeypatch.setattr(rag_search, "rerank", _rerank)
    monkeypatch.setattr(
        rag_search, "_audit", lambda claims, action, detail: audits.append((action, detail))
    )


def _counter_value(counter, **labels) -> float:
    c = counter.labels(**labels) if labels else counter
    return c._value.get()


class TestAuditTimings:
    async def test_every_stage_is_timed(self, monkeypatch):
        audits: list = []
        _stub(monkeypatch, audits)

        await rag_search.run_rag_search("Bearer t", "a query")

        timings = audits[0][1]["timings_ms"]
        assert set(timings) == {"embed", "retrieve", "rerank", "total"}
        assert all(isinstance(v, int) for v in timings.values()), "whole ms, not floats"

    async def test_total_is_at_least_the_sum_of_its_parts(self, monkeypatch):
        audits: list = []
        _stub(monkeypatch, audits)

        await rag_search.run_rag_search("Bearer t", "a query")

        t = audits[0][1]["timings_ms"]
        assert t["total"] >= max(t["embed"], t["retrieve"], t["rerank"])

    async def test_timings_are_recorded_when_no_chunks_match(self, monkeypatch):
        """An empty result is exactly when you want to know where the time
        went -- the filter still ran and the embedding still cost something."""
        audits: list = []
        _stub(monkeypatch, audits, hits=[])

        await rag_search.run_rag_search("Bearer t", "a query")

        assert "timings_ms" in audits[0][1]

    async def test_timings_never_reach_the_caller(self, monkeypatch):
        """Latency correlates with how much the access filter matched, so
        per-stage figures in the response would sharpen membership inference
        (#127). Operators get them via the audit log; callers do not."""
        audits: list = []
        _stub(monkeypatch, audits)

        result = await rag_search.run_rag_search("Bearer t", "a query")

        assert "timings_ms" not in result
        assert "timings" not in str(result)


class TestMetrics:
    async def test_outcomes_are_counted(self, monkeypatch):
        before = _counter_value(metrics.queries_total, outcome="ok")
        audits: list = []
        _stub(monkeypatch, audits)

        await rag_search.run_rag_search("Bearer t", "a query")

        assert _counter_value(metrics.queries_total, outcome="ok") == before + 1

    async def test_denied_query_is_counted_separately(self, monkeypatch):
        class _NoQuery(_Claims):
            can_query = False

        before = _counter_value(metrics.queries_total, outcome="denied")
        monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _NoQuery())
        monkeypatch.setattr(rag_search, "_audit", lambda *_a: None)

        await rag_search.run_rag_search("Bearer t", "a query")

        assert _counter_value(metrics.queries_total, outcome="denied") == before + 1

    async def test_reranker_fallback_is_counted(self, monkeypatch):
        """FR-25 degrades to fused order instead of failing, so without this
        counter a ranking-quality drop is invisible."""
        before = _counter_value(metrics.reranker_fallback_total)
        audits: list = []
        _stub(
            monkeypatch,
            audits,
            rerank_note="reranker-service unavailable (boom); using fused order",
        )

        await rag_search.run_rag_search("Bearer t", "a query")

        assert _counter_value(metrics.reranker_fallback_total) == before + 1

    async def test_healthy_rerank_does_not_count_as_fallback(self, monkeypatch):
        before = _counter_value(metrics.reranker_fallback_total)
        audits: list = []
        _stub(monkeypatch, audits)

        await rag_search.run_rag_search("Bearer t", "a query")

        assert _counter_value(metrics.reranker_fallback_total) == before


class TestScrapeEndpoint:
    def _request(self):
        return Request({"type": "http", "method": "GET", "path": "/metrics", "headers": []})

    async def test_returns_prometheus_text(self):
        resp = await server.prometheus_metrics(self._request())

        assert resp.status_code == 200
        assert "text/plain" in resp.media_type
        assert b"nexus_rag_query_stage_seconds" in resp.body

    async def test_exposes_the_metrics_72_asks_for(self):
        resp = await server.prometheus_metrics(self._request())

        for name in (
            b"nexus_rag_query_stage_seconds",
            b"nexus_rag_queries_total",
            b"nexus_rag_reranker_fallback_total",
            b"nexus_rag_results_returned",
        ):
            assert name in resp.body, f"{name!r} missing from the scrape payload"

    @pytest.mark.parametrize("leak", [b"bob-query", b"deployment schedule", b"d1"])
    async def test_scrape_payload_carries_no_identity_or_content(self, monkeypatch, leak):
        """Metrics are typically far more widely readable than the corpus. A
        per-user or per-query label would rebuild the surveillance surface
        #125 removed from the audit log."""
        audits: list = []
        _stub(monkeypatch, audits)
        await rag_search.run_rag_search("Bearer t", "deployment schedule")

        resp = await server.prometheus_metrics(self._request())

        assert leak not in resp.body
