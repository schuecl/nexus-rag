"""FR-3..FR-6, moved out of ingestion-api's in-process BackgroundTasks
(NFR-11) into a durable, crash-recoverable JetStream consumer -- a worker
crash or restart mid-processing no longer silently strands a document in
`processing` forever. Un-acking a message on a transient failure is what
actually provides that durability: JetStream redelivers it to another
attempt (this worker's next poll, or a different replica entirely) after
its ack-wait timeout, which BackgroundTasks had no equivalent of at all.

Terminal outcomes (success, or a permanent failure like unparseable input)
are acked so the message is never redelivered pointlessly; only genuinely
unexpected/transient errors (Qdrant or the DB unreachable, etc.) are left
un-acked.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import ConsumerConfig
from qdrant_client.models import PointStruct
from sqlmodel import Session

from app.chunking import chunk_sections
from app.embedding import EMBEDDING_MODEL, EmbeddingError, embed_texts
from app.parsing import ParsingError, parse_document
from common.db import get_engine
from common.job_queue import INGESTION_SUBJECT, ensure_stream, get_nats_connection
from common.models import AuditLogEntry, Document
from common.object_store import get_object_store
from common.qdrant_store import (
    EMBEDDING_MODEL_KEY,
    chunk_vector,
    ensure_collection,
    get_qdrant_client,
    upsert_chunks,
)
from common.sparse_embedding import embed_sparse
from common.tracing import extract_trace_context, get_tracer

logger = logging.getLogger("ingestion-worker")

# #134: spans carry ids, counts, and byte sizes only -- never chunk text,
# filenames, or any other corpus content (see common/tracing.py).
tracer = get_tracer("ingestion-worker")

# Generous enough to cover a slow embedding pass over a large document
# without a false-positive redelivery racing the attempt that's already
# in flight -- redelivery is meant for "the worker actually died", not
# "processing is still legitimately running".
ACK_WAIT_SECONDS = 300.0
DURABLE_CONSUMER_NAME = "ingestion-worker"
FETCH_BATCH_SIZE = 1
FETCH_TIMEOUT_SECONDS = 5.0

# Bound on redelivery of a message this worker keeps failing to process.
# Without it, a JetStream consumer redelivers forever: a permanent failure
# that doesn't happen to surface as ParsingError/EmbeddingError (the clearest
# case being an object-store key that can't be read) would be retried
# indefinitely, and with FETCH_BATCH_SIZE = 1 that means the worker never
# advances past it -- every attempt also re-running `doc.status = "processing"`
# + commit(), so it's a write loop against Postgres, not just a spin.
MAX_DELIVERY_ATTEMPTS = 5
# Delay applied when handing a message back for redelivery. `nak()` with no
# delay redelivers immediately, which turns a persistently-failing message
# into a hot loop; this spaces the retries out enough for a genuinely
# transient dependency (Qdrant, Postgres, Ollama) to come back.
NAK_BACKOFF_SECONDS = 30.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ConsumerStatus:
    """What /health reports about the consumer loop (app/main.py).

    `running` is the load-bearing field, and it is deliberately derived from
    "has consume_forever exited", not from a heartbeat freshness check: a
    single large document can legitimately occupy the loop for minutes (see
    ACK_WAIT_SECONDS), so a staleness threshold tight enough to notice a dead
    consumer quickly would also kill a healthy worker mid-document. The
    timestamps and counters below are reported for humans and dashboards, and
    nothing gates on them.
    """

    running: bool = False
    started_at: datetime | None = None
    last_poll_at: datetime | None = None
    processed: int = 0
    # Exception *type* only ("NoServersError"), never str(exc). This field is
    # served by /health, which takes no authentication and is published to the
    # host in the Compose stack (8004:8004), so a raw exception message would
    # hand any caller internal broker hostnames, ports, and stream names. The
    # type is enough to tell a human operator which way the consumer died;
    # the full traceback goes to the logs (consume_forever, and main.py's
    # done-callback), which are already an authenticated surface.
    stopped_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "processed": self.processed,
            "stopped_reason": self.stopped_reason,
        }


# Module-level so app/main.py's /health can read it without the consumer and
# the HTTP app having to share anything else.
STATUS = ConsumerStatus()


async def process_document(document_id: uuid.UUID) -> bool:
    """Returns True for a terminal outcome (success or permanent failure --
    ack the message either way), False for a transient/unexpected error
    (don't ack -- let JetStream redeliver)."""
    with Session(get_engine()) as session:
        doc = session.get(Document, document_id)
        if doc is None:
            # Nothing sensible to retry -- the row is just gone (shouldn't
            # happen in practice). Ack so this doesn't loop forever.
            logger.error("document %s not found, acking to drop the message", document_id)
            return True

        doc.status = "processing"
        session.add(doc)
        session.commit()

        try:
            if doc.original_object_key is None:
                # A document can be purged (common/purge.py) while still
                # queued/processing -- original_object_key is cleared as part
                # of that. Same permanent-failure handling as a missing
                # object-store key below: retrying can't produce a key that
                # was deliberately destroyed.
                raise FileNotFoundError("original_object_key is unset (document was purged)")
            contents = get_object_store().get(doc.original_object_key)
            # #134's ingest.process stage spans: attribute values are counts
            # and byte sizes only, never the text they describe.
            with tracer.start_as_current_span("parse") as span:
                span.set_attribute("document.bytes", len(contents))
                sections = parse_document(doc.filename, contents)
                span.set_attribute("document.sections", len(sections))
            with tracer.start_as_current_span("chunk") as span:
                chunks = chunk_sections(sections)
                span.set_attribute("document.chunks", len(chunks))
            if not chunks:
                raise ParsingError("document contained no extractable text")

            with tracer.start_as_current_span("embed") as span:
                span.set_attribute("document.chunks", len(chunks))
                dense_vectors = await embed_texts([c.text for c in chunks])
                sparse_vectors = embed_sparse([c.text for c in chunks])

            points = [
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=chunk_vector(dense, sparse),
                    payload={
                        "document_id": str(doc.id),
                        "chunk_index": chunk.chunk_index,
                        "text": chunk.text,
                        "heading": chunk.heading,
                        "page_or_slide": chunk.page_or_slide,
                        # issue #89: lets reranking/retrieval weight chunks by
                        # what kind of content they hold (e.g. prefer table
                        # chunks for a query asking about a specific value).
                        "content_type": chunk.content_type,
                        # Issue #122: which model produced this vector, and
                        # when. Without it a change to EMBEDDING_MODEL leaves
                        # stored and query vectors in different embedding
                        # spaces with nothing to detect it -- the dense leg
                        # silently returns noise while BM25 and reranking keep
                        # the results looking plausible.
                        EMBEDDING_MODEL_KEY: EMBEDDING_MODEL,
                        "embedded_at": _utcnow().isoformat(),
                        "filename": doc.filename,
                        "doc_type": doc.doc_type,
                        "source_originator": doc.source_originator,
                        "classification": doc.classification,
                        "releasability": doc.releasability,
                        "access_scope": doc.access_scope,
                        # Written as pending_review directly (not doc.status,
                        # which is still `processing` at this point) -- this
                        # is what keeps the chunk excluded from retrieval
                        # (FR-11/FR-26) until a curator approves it.
                        "status": "pending_review",
                    },
                )
                for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True)
            ]
            with tracer.start_as_current_span("qdrant.upsert") as span:
                span.set_attribute("qdrant.points", len(points))
                qdrant = get_qdrant_client()
                ensure_collection(qdrant, dense_size=len(dense_vectors[0]))
                upsert_chunks(qdrant, points)

            doc.status = "embedded"
            doc.chunk_count = len(chunks)
            session.add(doc)
            session.commit()

            doc.status = "pending_review"
            session.add(doc)
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.embedded",
                    target_id=str(doc.id),
                    detail={"filename": doc.filename, "chunk_count": doc.chunk_count},
                )
            )
            session.commit()
            return True
        except FileNotFoundError as exc:
            # The original upload isn't in the object store. Retrying reads of
            # a key that isn't there can't succeed, so treat it the same as
            # unparseable input rather than letting it consume the redelivery
            # budget. Covers FilesystemObjectStore (the dev backend); the S3
            # backend surfaces a missing key as botocore's ClientError, which
            # still falls through to the transient branch below and is bounded
            # by MAX_DELIVERY_ATTEMPTS instead.
            doc.status = "failed"
            doc.processing_error = f"original file missing from object store: {exc}"
            session.add(doc)
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.failed",
                    target_id=str(doc.id),
                    detail={"error": doc.processing_error},
                )
            )
            session.commit()
            return True
        except (ParsingError, EmbeddingError) as exc:
            # Permanent failures -- corrupt/unsupported input, or the
            # embedding service rejecting this exact request outright.
            # Retrying the identical input wouldn't help, so land the
            # document in `failed` and ack rather than let JetStream
            # redeliver it forever.
            doc.status = "failed"
            doc.processing_error = str(exc)
            session.add(doc)
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.failed",
                    target_id=str(doc.id),
                    detail={"error": str(exc)},
                )
            )
            session.commit()
            return True
        except Exception:
            # Unexpected/transient -- Qdrant or the DB unreachable, a bug,
            # etc. Roll back rather than commit doc.status = "processing" as
            # a dead end, and don't ack: JetStream redelivers this message
            # to another attempt after ACK_WAIT_SECONDS.
            logger.exception(
                "transient failure processing document %s, leaving unacked for redelivery",
                document_id,
            )
            session.rollback()
            return False


def _mark_undeliverable(document_id: uuid.UUID, attempts: int) -> None:
    """Land a document that exhausted its redelivery budget in `failed`, so
    the uploader gets the FR-8 status they'd otherwise never see -- without
    this, a message dropped after MAX_DELIVERY_ATTEMPTS leaves the row stuck
    at `processing` with nothing to explain it."""
    try:
        with Session(get_engine()) as session:
            doc = session.get(Document, document_id)
            if doc is None:
                return
            doc.status = "failed"
            doc.processing_error = (
                f"processing failed after {attempts} delivery attempts; see ingestion-worker logs"
            )
            session.add(doc)
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.failed",
                    target_id=str(doc.id),
                    detail={"error": doc.processing_error, "delivery_attempts": attempts},
                )
            )
            session.commit()
    except Exception:
        logger.exception(
            "could not mark document %s failed after exhausting redelivery", document_id
        )


async def _handle_message(msg: Msg) -> None:
    """One message, start to finish. Every failure mode here is contained:
    nothing raised while handling a single message may unwind consume_forever
    and take the whole consumer down with it."""
    raw = msg.data.decode(errors="replace")
    try:
        document_id = uuid.UUID(raw)
    except ValueError:
        # Not retryable -- redelivering the same unparseable payload can only
        # produce the same result. Previously this raised straight out of the
        # loop and killed the consumer.
        logger.error("dropping message with unparseable document id %r", raw)
        await msg.term()
        return

    # #134: parent this consumer's span onto the publisher's trace via the
    # message headers, so ingest.submit -> ingest.process reads as one trace
    # even across pods and redeliveries (JetStream stores headers with the
    # message). A missing header -- an in-flight message from before this
    # existed, or an untraced publisher -- just starts a fresh trace.
    with tracer.start_as_current_span(
        "ingest.process",
        context=extract_trace_context(msg.headers),
        attributes={
            "document.id": str(document_id),
            "messaging.delivery_attempt": msg.metadata.num_delivered,
        },
    ) as span:
        terminal = await process_document(document_id)
        span.set_attribute("ingest.terminal", terminal)
    if terminal:
        await msg.ack()
        STATUS.processed += 1
        return

    # Transient: hand it back for redelivery, but bounded. num_delivered
    # counts this attempt, so `>=` is the last-attempt test.
    attempts = msg.metadata.num_delivered
    if attempts >= MAX_DELIVERY_ATTEMPTS:
        logger.error(
            "document %s failed %d delivery attempts, giving up and marking it failed",
            document_id,
            attempts,
        )
        _mark_undeliverable(document_id, attempts)
        await msg.term()
        return

    logger.warning(
        "document %s failed attempt %d/%d, redelivering in %ss",
        document_id,
        attempts,
        MAX_DELIVERY_ATTEMPTS,
        NAK_BACKOFF_SECONDS,
    )
    await msg.nak(delay=NAK_BACKOFF_SECONDS)


async def consume_forever() -> None:
    try:
        nc = await get_nats_connection()
        js = nc.jetstream()
        await ensure_stream(js)
        psub = await js.pull_subscribe(
            INGESTION_SUBJECT,
            durable=DURABLE_CONSUMER_NAME,
            config=ConsumerConfig(ack_wait=ACK_WAIT_SECONDS, max_deliver=MAX_DELIVERY_ATTEMPTS),
        )

        logger.info("ingestion-worker: subscribed, waiting for jobs")
        STATUS.running = True
        STATUS.started_at = _utcnow()

        while True:
            STATUS.last_poll_at = _utcnow()
            try:
                msgs = await psub.fetch(FETCH_BATCH_SIZE, timeout=FETCH_TIMEOUT_SECONDS)
            except NatsTimeoutError:
                continue  # no jobs waiting -- normal, just poll again

            for msg in msgs:
                try:
                    await _handle_message(msg)
                except Exception:
                    logger.exception("unhandled error handling a message, continuing")
    except asyncio.CancelledError:
        # Normal shutdown -- app/main.py's lifespan cancels this task.
        STATUS.stopped_reason = "cancelled"
        raise
    except BaseException as exc:
        # Anything else means the consumer is gone and ingestion has silently
        # stopped. Record it so /health can fail instead of reporting "ok"
        # from an HTTP app that outlives its only real workload. Type only --
        # see ConsumerStatus.stopped_reason for why the message stays in the
        # logs and out of the response body.
        STATUS.stopped_reason = type(exc).__name__
        logger.exception("ingestion-worker consumer exited unexpectedly")
        raise
    finally:
        STATUS.running = False
