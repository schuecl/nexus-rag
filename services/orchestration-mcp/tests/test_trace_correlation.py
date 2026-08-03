"""Coverage for issue #363: an FR-31 audit query row must carry the #134
trace id that produced it, so an audit entry stops being a dead end -- an
investigator can go from "this audit row" to the exact embed/vector.query/
rerank spans behind it.
"""

from __future__ import annotations

from contextlib import contextmanager

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from qdrant_client.models import SparseVector

from app import rag_search


class _Claims:
    sub = "u1"
    preferred_username = "bob-query"
    clearance = "SECRET"
    groups = ()
    org = "USAREUR-AF"
    can_query = True
    releasability = ()


class _Hit:
    def __init__(self):
        self.id = "c1"
        self.score = 0.9
        self.payload = {"document_id": "d1", "text": "body", "content_type": "text"}


def _stub(monkeypatch, audits: list, *, hits=None):
    @contextmanager
    def _session():
        yield object()

    class _Store:
        def hybrid_query(self, **_kw):
            return hits if hits is not None else [_Hit()]

        def access_filter_summary(self, _claims, _allowed):
            return {"backend": "fake"}

        def stored_embedding_model(self):
            return None

    async def _embed(_q):
        return [0.0]

    async def _rerank(_q, candidates, top_k, **_kw):
        return candidates[:top_k], "cross-encoder rerank"

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


class TestTraceIdInAuditDetail:
    async def test_absent_when_no_span_is_active(self, monkeypatch):
        """Default test process: no TracerProvider configured (#134's
        no-op), so there's no trace to correlate -- the field must not
        appear as a fake/zeroed id."""
        audits: list = []
        _stub(monkeypatch, audits)

        await rag_search.run_rag_search("Bearer t", "a query")

        assert "trace_id" not in audits[0][1]

    async def test_matches_the_active_trace_when_sampled(self, monkeypatch):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        real_tracer = provider.get_tracer("test")
        monkeypatch.setattr(rag_search, "tracer", real_tracer)
        audits: list = []
        _stub(monkeypatch, audits)

        await rag_search.run_rag_search("Bearer t", "a query")

        root_span = next(s for s in exporter.get_finished_spans() if s.name == "rag_search")
        expected = format(root_span.context.trace_id, "032x")
        assert audits[0][1]["trace_id"] == expected

    async def test_denied_query_still_correlates(self, monkeypatch):
        """A denied attempt is exactly the case an investigator most wants a
        trace pointer for -- what was reached for, and by what request."""
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        real_tracer = provider.get_tracer("test")
        monkeypatch.setattr(rag_search, "tracer", real_tracer)

        class _NoQuery(_Claims):
            can_query = False

        audits: list = []
        monkeypatch.setattr(rag_search, "parse_claims", lambda _t: _NoQuery())
        monkeypatch.setattr(
            rag_search, "_audit", lambda claims, action, detail: audits.append((action, detail))
        )

        await rag_search.run_rag_search("Bearer t", "a query")

        assert audits[0][0] == "query.denied"
        root_span = next(s for s in exporter.get_finished_spans() if s.name == "rag_search")
        expected = format(root_span.context.trace_id, "032x")
        assert audits[0][1]["trace_id"] == expected

    async def test_no_chunks_matched_still_correlates(self, monkeypatch):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        real_tracer = provider.get_tracer("test")
        monkeypatch.setattr(rag_search, "tracer", real_tracer)
        audits: list = []
        _stub(monkeypatch, audits, hits=[])

        await rag_search.run_rag_search("Bearer t", "a query")

        root_span = next(s for s in exporter.get_finished_spans() if s.name == "rag_search")
        expected = format(root_span.context.trace_id, "032x")
        assert audits[0][1]["trace_id"] == expected
