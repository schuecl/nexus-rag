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

import logging
import uuid

from app.chunking import chunk_sections
from app.embedding import EmbeddingError, embed_texts
from app.parsing import ParsingError, parse_document
from common.db import get_engine
from common.job_queue import INGESTION_SUBJECT, ensure_stream, get_nats_connection
from common.models import AuditLogEntry, Document
from common.object_store import get_object_store
from common.qdrant_store import chunk_vector, ensure_collection, get_qdrant_client, upsert_chunks
from common.sparse_embedding import embed_sparse
from nats.js.api import ConsumerConfig
from nats.errors import TimeoutError as NatsTimeoutError
from qdrant_client.models import PointStruct
from sqlmodel import Session

logger = logging.getLogger("ingestion-worker")

# Generous enough to cover a slow embedding pass over a large document
# without a false-positive redelivery racing the attempt that's already
# in flight -- redelivery is meant for "the worker actually died", not
# "processing is still legitimately running".
ACK_WAIT_SECONDS = 300.0
DURABLE_CONSUMER_NAME = "ingestion-worker"
FETCH_BATCH_SIZE = 1
FETCH_TIMEOUT_SECONDS = 5.0


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
            contents = get_object_store().get(doc.original_object_key)
            sections = parse_document(doc.filename, contents)
            chunks = chunk_sections(sections)
            if not chunks:
                raise ParsingError("document contained no extractable text")

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
                for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors)
            ]
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
        except Exception:  # noqa: BLE001 -- NFR-7: never crash the worker
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


async def consume_forever() -> None:
    nc = await get_nats_connection()
    js = nc.jetstream()
    await ensure_stream(js)
    psub = await js.pull_subscribe(
        INGESTION_SUBJECT,
        durable=DURABLE_CONSUMER_NAME,
        config=ConsumerConfig(ack_wait=ACK_WAIT_SECONDS),
    )

    logger.info("ingestion-worker: subscribed, waiting for jobs")
    while True:
        try:
            msgs = await psub.fetch(FETCH_BATCH_SIZE, timeout=FETCH_TIMEOUT_SECONDS)
        except NatsTimeoutError:
            continue  # no jobs waiting -- normal, just poll again

        for msg in msgs:
            document_id = uuid.UUID(msg.data.decode())
            terminal = await process_document(document_id)
            if terminal:
                await msg.ack()
            else:
                await msg.nak()
