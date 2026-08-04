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
# Issue #92: image captioning is degrade-on-failure (app/captioning.py), so
# these counters are the visibility that replaces a hard error -- a rising
# skip rate is how an operator learns figures are dropping out of the corpus.
images_captioned_total = Counter(
    "nexus_rag_ingestion_worker_images_captioned_total",
    "Embedded images successfully captioned at ingestion.",
)
images_skipped_total = Counter(
    "nexus_rag_ingestion_worker_images_skipped_total",
    "Embedded images not captioned, by reason.",
    ["reason"],
)
# Issue #308: LLM classification suggestion is also degrade-on-failure
# (app/classification_suggestion.py) -- this counter is the visibility that
# replaces a hard error, same rationale as images_skipped_total above.
llm_suggestions_total = Counter(
    "nexus_rag_ingestion_worker_llm_suggestions_total",
    "LLM zero-shot classification suggestion attempts, by outcome.",
    ["outcome"],
)
# Issue #343 (Phase 2 of #342): the LLM-assisted PII/sensitive-info pass is
# also degrade-on-failure (app/pii_llm_advisory.py) -- same visibility
# rationale as llm_suggestions_total above.
pii_llm_findings_total = Counter(
    "nexus_rag_ingestion_worker_pii_llm_findings_total",
    "LLM-assisted PII/sensitive-info advisory attempts, by outcome.",
    ["outcome"],
)
# Issue #378: separate counter from pii_llm_findings_total above -- that one
# tracks the additive context-dependent-findings pass, this tracks the
# verification pass over Phase 1's own regex findings. Sharing one counter
# with two purposes would make "outcome=unavailable" ambiguous about which
# half of the PII_LLM_MODEL call budget failed.
pii_llm_verification_total = Counter(
    "nexus_rag_ingestion_worker_pii_llm_verification_total",
    "LLM verification attempts over Phase 1 regex PII findings, by outcome.",
    ["outcome"],
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
