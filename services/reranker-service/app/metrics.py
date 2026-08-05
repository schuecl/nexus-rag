"""Prometheus metrics for cross-encoder serving."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

requests_total = Counter(
    "nexus_rag_reranker_requests_total",
    "Reranker requests by outcome.",
    ["outcome"],
)
model_predict_seconds = Histogram(
    "nexus_rag_reranker_model_predict_seconds",
    "Cross-encoder prediction time.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
batch_chunks = Histogram(
    "nexus_rag_reranker_batch_chunks",
    "Chunks scored per reranker request.",
    buckets=(0, 1, 2, 5, 10, 20, 50, 100, 200, 512),
)
model_loaded = Gauge(
    "nexus_rag_reranker_model_loaded",
    "1 once the cross-encoder model is loaded.",
)
oversized_chunks_total = Counter(
    "nexus_rag_reranker_oversized_chunks_total",
    "Chunks whose (query, chunk) pair exceeded the model's input window "
    "(issue #393), by how they were handled: 'windowed' = scored as the max "
    "over overlapping in-window pieces, 'truncated' = head-truncated by the "
    "tokenizer (RERANKER_WINDOW_SCORING=false).",
    ["handling"],
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
