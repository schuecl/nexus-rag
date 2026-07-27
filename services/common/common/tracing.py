"""Distributed tracing across the ingestion queue and retrieval fan-out (#134).

Two flows cross process boundaries and were invisible as units: ingestion
(ingestion-api -> NATS JetStream -> ingestion-worker, possibly on another pod,
possibly after a redelivery) and retrieval (orchestration-mcp -> Ollama /
Qdrant / reranker-service). #72's per-stage timings measure each stage from
inside one function; this module ties the stages into one trace:

    ingest.submit   (ingestion-api)
    └─ ingest.process  (ingestion-worker, joined via NATS message headers)
       ├─ parse
       ├─ chunk
       ├─ embed          -> Ollama (httpx auto-instrumented)
       └─ qdrant.upsert

    rag_search      (orchestration-mcp)
    ├─ embed.query       -> Ollama
    ├─ qdrant.query      (dense + sparse prefetch)
    └─ rerank            -> reranker-service (context propagates over httpx)

The deliberate design point is the queue boundary: the W3C traceparent rides
in NATS *message headers* (inject_trace_context / extract_trace_context), not
the body -- the body stays a bare document id, so #109's malformed-payload
guard and in-flight messages across an upgrade are untouched. JetStream
stores headers with the message, so redelivery carries the same context
(validated live; see tests and the PR).

Two constraints this repo's own issues impose, enforced here by convention
and documented at every call site:

- Span attributes carry ids, counts, sizes, and durations -- NEVER corpus
  content or query text. Traces land in a store with broader access than the
  corpus; putting the query in a span would rebuild exactly what #125
  removed from the audit log and #72 kept out of metric labels.

- Sampling is configurable and defaults LOW (5%). An always-on tracer taxes
  a latency-sensitive path (NFR-4 has no budget yet). ParentBased means a
  sampled request stays sampled across every service it touches, and an
  unsampled one costs nothing downstream either.

Configuration (standard OpenTelemetry variables; all optional -- unset
endpoint means tracing is disabled and every helper degrades to a no-op):

    OTEL_EXPORTER_OTLP_ENDPOINT  OTLP/HTTP collector base URL (e.g. a local
                                 otel-collector or Tempo); unset = disabled
    OTEL_TRACES_SAMPLER_ARG      head-sampling ratio 0..1 (default 0.05)
    OTEL_SERVICE_NAME            overrides the reported service.name
                                 (default nexus-rag-<service>)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

if TYPE_CHECKING:
    from opentelemetry.context import Context

logger = logging.getLogger("tracing")

_DEFAULT_SAMPLE_RATIO = 0.05

# Idempotence guard: the global TracerProvider can only be set once per
# process; repeated setup_tracing() calls (tests, reloads) must not stack
# providers or exporters.
_state: dict = {"configured": False, "enabled": False}


def _sample_ratio() -> float:
    raw = os.environ.get("OTEL_TRACES_SAMPLER_ARG", "").strip()
    if not raw:
        return _DEFAULT_SAMPLE_RATIO
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 0.0 <= value <= 1.0:
        logger.warning(
            "OTEL_TRACES_SAMPLER_ARG=%r is not a ratio in 0..1; using %s",
            raw,
            _DEFAULT_SAMPLE_RATIO,
        )
        return _DEFAULT_SAMPLE_RATIO
    return value


def setup_tracing(service: str) -> bool:
    """Configure tracing for this process; call once at service startup.

    Returns True when tracing is active. Disabled (False) when
    OTEL_EXPORTER_OTLP_ENDPOINT is unset -- the OpenTelemetry API's default
    no-op tracer then makes every span in the codebase free, so call sites
    never need to guard.
    """
    if _state["configured"]:
        return _state["enabled"]
    _state["configured"] = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT is not set)")
        _state["enabled"] = False
        return False

    ratio = _sample_ratio()
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.environ.get("OTEL_SERVICE_NAME", f"nexus-rag-{service}")}
        ),
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT itself (appending the
    # standard /v1/traces path); BatchSpanProcessor keeps exporting off the
    # request path.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _state["enabled"] = True
    logger.info("tracing enabled: OTLP to %s, head-sampling ratio %s (#134)", endpoint, ratio)
    return True


def get_tracer(name: str) -> trace.Tracer:
    """The tracer call sites use; a no-op tracer when tracing is disabled."""
    return trace.get_tracer(name)


def inject_trace_context() -> dict[str, str] | None:
    """Serialize the current span context into a headers dict (W3C
    traceparent/tracestate) for the NATS message, or None when there is no
    active recorded span -- js.publish(headers=None) is the unchanged wire
    shape, so a tracing-disabled publisher emits byte-identical messages.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier or None


def extract_trace_context(headers: dict[str, str] | None) -> Context | None:
    """Rebuild the publisher's span context from NATS message headers, for
    the consumer to parent its ingest.process span onto. None (no headers, or
    a message published before tracing existed / by an untraced publisher)
    means "start a fresh trace" -- old in-flight messages keep working.
    """
    if not headers:
        return None
    return propagate.extract(headers)
