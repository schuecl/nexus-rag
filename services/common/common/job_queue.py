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

NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
NATS_AUTH_TOKEN = os.environ.get("NATS_AUTH_TOKEN", "dev-nats-token")

INGESTION_STREAM = "INGESTION_JOBS"
INGESTION_SUBJECT = "ingestion.jobs"


async def get_nats_connection() -> nats.NATS:
    return await nats.connect(servers=[NATS_URL], token=NATS_AUTH_TOKEN)


async def ensure_stream(js: JetStreamContext) -> None:
    """Idempotent -- create the ingestion-jobs stream if it doesn't already
    exist. Matches common/qdrant_store.py's ensure_collection() pattern:
    called by whichever caller happens to run first (ingestion-api at
    publish time, or ingestion-worker at consumer-startup time), safe
    either way."""
    try:
        await js.stream_info(INGESTION_STREAM)
    except nats.js.errors.NotFoundError:
        await js.add_stream(config=StreamConfig(name=INGESTION_STREAM, subjects=[INGESTION_SUBJECT]))


async def publish_ingestion_job(js: JetStreamContext, document_id: str) -> None:
    await ensure_stream(js)
    await js.publish(INGESTION_SUBJECT, document_id.encode())
