"""Unit tests for common.job_queue -- NFR-11 JetStream publishing. Uses a fake
JetStream context; no live NATS. Redelivery/ack semantics are covered by the
compose-level e2e, not here.
"""

from __future__ import annotations

import nats.js.errors

from common.job_queue import (
    INGESTION_STREAM,
    INGESTION_SUBJECT,
    ensure_stream,
    publish_ingestion_job,
)


class _FakeJetStream:
    def __init__(self, *, stream_exists: bool):
        self.stream_exists = stream_exists
        self.added_streams: list[dict] = []
        self.published: list[tuple[str, bytes]] = []
        self.published_headers: list[dict | None] = []

    async def stream_info(self, name: str):
        if not self.stream_exists:
            raise nats.js.errors.NotFoundError()
        return object()

    async def add_stream(self, *, config):
        self.added_streams.append({"name": config.name, "subjects": config.subjects})
        self.stream_exists = True

    async def publish(self, subject: str, payload: bytes, headers=None):
        # #134: the traceparent rides in headers; the body must stay a bare
        # document id (asserted below).
        self.published.append((subject, payload))
        self.published_headers.append(headers)


class TestEnsureStream:
    async def test_creates_stream_when_missing(self):
        js = _FakeJetStream(stream_exists=False)
        await ensure_stream(js)
        assert js.added_streams == [{"name": INGESTION_STREAM, "subjects": [INGESTION_SUBJECT]}]

    async def test_idempotent_when_present(self):
        js = _FakeJetStream(stream_exists=True)
        await ensure_stream(js)
        assert js.added_streams == []


class TestPublishIngestionJob:
    async def test_publishes_document_id_bytes_to_subject(self):
        js = _FakeJetStream(stream_exists=True)
        await publish_ingestion_job(js, "doc-123")
        assert js.published == [(INGESTION_SUBJECT, b"doc-123")]

    async def test_ensures_stream_before_publishing(self):
        js = _FakeJetStream(stream_exists=False)
        await publish_ingestion_job(js, "doc-123")
        assert js.added_streams  # stream created first
        assert js.published == [(INGESTION_SUBJECT, b"doc-123")]

    async def test_payload_carries_only_the_document_id(self):
        # The original file lives in the object store, never in the message --
        # keeps the queue small regardless of upload size.
        js = _FakeJetStream(stream_exists=True)
        await publish_ingestion_job(js, "doc-123")
        _, payload = js.published[0]
        assert payload == b"doc-123"
        assert len(payload) < 64

    async def test_document_id_is_the_jetstream_deduplication_key(self):
        js = _FakeJetStream(stream_exists=True)

        await publish_ingestion_job(js, "doc-123")

        assert js.published_headers[0]["Nats-Msg-Id"] == "doc-123"
