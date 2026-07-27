"""#134: trace context must survive the NATS queue boundary.

These tests build a real (in-memory) TracerProvider rather than mocking the
OpenTelemetry API: what matters is that the exact headers
publish_ingestion_job puts on the wire reconstruct the same trace on the
consumer side, and that a tracing-disabled publisher emits the unchanged
pre-#134 wire shape (headers=None, bare-uuid body)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from common import tracing
from common.job_queue import INGESTION_SUBJECT, publish_ingestion_job
from common.tracing import extract_trace_context, inject_trace_context


@pytest.fixture
def recording_tracer():
    """A real SDK tracer wired to an in-memory exporter, without touching the
    process-global provider (which can only be set once)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


class TestQueueBoundaryPropagation:
    def test_inject_extract_round_trip_preserves_the_trace(self, recording_tracer):
        tracer, _ = recording_tracer
        with tracer.start_as_current_span("ingest.submit") as publish_span:
            headers = inject_trace_context()
        assert headers is not None
        assert "traceparent" in headers

        # Consumer side: the extracted context parents a new span onto the
        # publisher's trace -- same trace id, publisher's span as parent.
        ctx = extract_trace_context(headers)
        with tracer.start_as_current_span("ingest.process", context=ctx) as consumer_span:
            assert (
                consumer_span.get_span_context().trace_id
                == publish_span.get_span_context().trace_id
            )
            assert consumer_span.parent.span_id == publish_span.get_span_context().span_id

    def test_no_active_span_means_no_headers(self):
        # Tracing disabled (or no span in flight): inject returns None, so
        # js.publish(headers=None) keeps the pre-#134 wire shape.
        assert inject_trace_context() is None

    def test_extract_of_missing_or_foreign_headers_is_none_or_fresh(self):
        assert extract_trace_context(None) is None
        assert extract_trace_context({}) is None
        # Headers without a traceparent (e.g. NATS internal headers on a
        # message from an untraced publisher): extraction yields a context
        # that simply starts a fresh trace -- it must not raise.
        ctx = extract_trace_context({"Nats-Expected-Stream": "INGESTION_JOBS"})
        assert ctx is not None

    @pytest.mark.asyncio
    async def test_publish_carries_traceparent_in_headers_not_body(self, recording_tracer):
        tracer, _ = recording_tracer
        js = AsyncMock()
        with tracer.start_as_current_span("ingest.submit"):
            await publish_ingestion_job(js, "8ec9a2a6-0000-0000-0000-000000000001")
        subject, body = js.publish.await_args.args
        headers = js.publish.await_args.kwargs["headers"]
        assert subject == INGESTION_SUBJECT
        # #109's guard depends on the body staying a bare uuid.
        assert body == b"8ec9a2a6-0000-0000-0000-000000000001"
        assert "traceparent" in headers

    @pytest.mark.asyncio
    async def test_publish_without_tracing_sends_only_deduplication_header(self):
        js = AsyncMock()
        document_id = "8ec9a2a6-0000-0000-0000-000000000002"
        await publish_ingestion_job(js, document_id)
        assert js.publish.await_args.kwargs["headers"] == {"Nats-Msg-Id": document_id}


class TestSetup:
    @pytest.fixture(autouse=True)
    def _reset_state(self):
        before = dict(tracing._state)
        tracing._state.update({"configured": False, "enabled": False})
        yield
        tracing._state.update(before)

    def test_disabled_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert tracing.setup_tracing("ingestion-api") is False
        # And the no-op tracer makes spans free for every call site.
        span = tracing.get_tracer("t").start_span("anything")
        assert not span.get_span_context().trace_flags.sampled

    def test_sample_ratio_default_is_low(self, monkeypatch):
        monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)
        assert tracing._sample_ratio() == 0.05

    @pytest.mark.parametrize("raw", ["nonsense", "-0.5", "1.5"])
    def test_invalid_ratio_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", raw)
        assert tracing._sample_ratio() == 0.05

    def test_valid_ratio_is_used(self, monkeypatch):
        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "1.0")
        assert tracing._sample_ratio() == 1.0
