"""Low-cardinality Prometheus metrics for the ingestion and curation API.

Metrics deliberately contain route templates, status codes, outcomes, counts,
and byte sizes only. User names, document ids, filenames, and metadata never
become labels: the scrape surface is operational data, not a second audit log.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

http_requests_total = Counter(
    "nexus_rag_ingestion_api_http_requests_total",
    "HTTP requests handled by ingestion-api.",
    ["method", "route", "status"],
)
http_request_seconds = Histogram(
    "nexus_rag_ingestion_api_http_request_seconds",
    "HTTP request duration in ingestion-api.",
    ["method", "route"],
    buckets=_HTTP_BUCKETS,
)
http_requests_in_progress = Gauge(
    "nexus_rag_ingestion_api_http_requests_in_progress",
    "HTTP requests currently executing in ingestion-api.",
)

submissions_total = Counter(
    "nexus_rag_ingestion_submissions_total",
    "Document submissions by durable queue hand-off outcome.",
    ["outcome"],
)
upload_bytes = Histogram(
    "nexus_rag_ingestion_upload_bytes",
    "Accepted upload size in bytes.",
    buckets=(1_024, 10_240, 102_400, 1_048_576, 10_485_760, 52_428_800),
)
queue_publish_total = Counter(
    "nexus_rag_ingestion_queue_publish_total",
    "JetStream publication attempts by source and outcome.",
    ["source", "outcome"],
)
queue_reconciliation_pending = Gauge(
    "nexus_rag_ingestion_queue_reconciliation_pending",
    "Queued documents lacking an acknowledged JetStream publication.",
)
queue_oldest_unpublished_seconds = Gauge(
    "nexus_rag_ingestion_queue_oldest_unpublished_seconds",
    "Age of the oldest queued document without an acknowledged publication.",
)
curation_decisions_total = Counter(
    "nexus_rag_curation_decisions_total",
    "Curation decisions by outcome.",
    ["decision"],
)


async def http_metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path in {"/metrics", "/health"}:
        return await call_next(request)

    started = perf_counter()
    http_requests_in_progress.inc()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        http_requests_in_progress.dec()
        route_obj = request.scope.get("route")
        # Route templates are bounded; raw paths may contain document ids.
        route = getattr(route_obj, "path", None) or "unmatched"
        method = request.method
        http_requests_total.labels(method=method, route=route, status=str(status)).inc()
        http_request_seconds.labels(method=method, route=route).observe(perf_counter() - started)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
