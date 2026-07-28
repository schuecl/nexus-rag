"""Prometheus metrics for durable ingestion processing."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

jobs_total = Counter(
    "nexus_rag_ingestion_worker_jobs_total",
    "Ingestion job attempts by outcome.",
    ["outcome"],
)
job_seconds = Histogram(
    "nexus_rag_ingestion_worker_job_seconds",
    "End-to-end duration of an ingestion processing attempt.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
stage_seconds = Histogram(
    "nexus_rag_ingestion_worker_stage_seconds",
    "Duration of parse, chunk, embed, and vector upsert stages.",
    ["stage"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
document_bytes = Histogram(
    "nexus_rag_ingestion_worker_document_bytes",
    "Original document bytes read by the worker.",
    buckets=(1_024, 10_240, 102_400, 1_048_576, 10_485_760, 52_428_800),
)
chunks_produced = Histogram(
    "nexus_rag_ingestion_worker_chunks_produced",
    "Chunks produced per successfully parsed document.",
    buckets=(0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1_000),
)
delivery_attempts = Histogram(
    "nexus_rag_ingestion_worker_delivery_attempts",
    "JetStream delivery number observed by the worker.",
    buckets=(1, 2, 3, 4, 5),
)
consumer_running = Gauge(
    "nexus_rag_ingestion_worker_consumer_running",
    "1 while the durable consumer loop is running.",
)
last_poll_timestamp_seconds = Gauge(
    "nexus_rag_ingestion_worker_last_poll_timestamp_seconds",
    "Unix timestamp of the most recent JetStream poll.",
)
last_success_timestamp_seconds = Gauge(
    "nexus_rag_ingestion_worker_last_success_timestamp_seconds",
    "Unix timestamp of the most recent successfully embedded document.",
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
