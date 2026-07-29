"""NFR-11: durable ingestion job queue, backed by NATS JetStream. Shared by
ingestion-api (publishes a job once a document is durably staged --
common/object_store.py -- and its Document row committed) and the
ingestion-worker service (subscribes as a durable consumer, runs
FR-3..FR-6, acks only on a terminal outcome) -- a worker crash or restart
mid-processing must not silently strand a document (NFR-11's whole point),
which is exactly what redelivery of an un-acked JetStream message gives us
for free.

Publishing carries only a document_id -- the original file lives in the
object store (common/object_store.py), not the message payload, so this
stays small regardless of upload size.
"""

from __future__ import annotations

import os

import nats
from nats.js.api import StreamConfig
from nats.js.client import JetStreamContext

from common.tracing import inject_trace_context

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
# Issue #212: NATS_USER/NATS_PASSWORD replace the single shared
# NATS_AUTH_TOKEN both services used to connect with -- ingestion-api and
# ingestion-worker each set these to their own credential (see
# infra/nats/nats.conf's per-user permissions), so this module stays
# agnostic to which caller it is; the split is entirely in which
# environment variables each deployment gives each service.
NATS_USER = os.environ.get("NATS_USER", "ingestion_api")
NATS_PASSWORD = os.environ.get("NATS_PASSWORD", "dev-nats-api-password")

INGESTION_STREAM = "INGESTION_JOBS"
INGESTION_SUBJECT = "ingestion.jobs"


async def get_nats_connection() -> nats.NATS:
    return await nats.connect(servers=[NATS_URL], user=NATS_USER, password=NATS_PASSWORD)


async def ensure_stream(js: JetStreamContext) -> None:
    """Idempotent -- create the ingestion-jobs stream if it doesn't already
    exist. Matches common/qdrant_store.py's ensure_collection() pattern:
    called by whichever caller happens to run first (ingestion-api at
    publish time, or ingestion-worker at consumer-startup time), safe
    either way."""
    try:
        await js.stream_info(INGESTION_STREAM)
    except nats.js.errors.NotFoundError:
        await js.add_stream(
            config=StreamConfig(name=INGESTION_STREAM, subjects=[INGESTION_SUBJECT])
        )


async def publish_ingestion_job(js: JetStreamContext, document_id: str) -> None:
    await ensure_stream(js)
    # #134: the W3C traceparent rides in message *headers*, never the body --
    # the body stays a bare document id, so ingestion-worker's
    # malformed-payload guard (#109) and any message already in flight across
    # an upgrade are untouched. JetStream stores headers with the message, so
    # a redelivery carries the same context. headers is None when tracing is
    # disabled, which is the byte-identical pre-#134 wire shape.
    headers = dict(inject_trace_context() or {})
    # JetStream de-duplicates this id inside the stream's duplicate window.
    # That covers the ambiguous-ack case where the server persisted a publish
    # but the API lost the acknowledgement and its reconciler retries. The
    # worker independently guards duplicate delivery beyond that window.
    headers["Nats-Msg-Id"] = document_id
    await js.publish(INGESTION_SUBJECT, document_id.encode(), headers=headers)
