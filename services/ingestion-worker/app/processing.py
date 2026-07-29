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
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import ConsumerConfig
from sqlmodel import Session, select

from app import metrics
from app.captioning import caption_images, captioning_enabled
from app.chunking import chunk_sections
from app.embedding import EMBEDDING_MODEL, EmbeddingError, embed_texts
from app.parsing import ParsedSection, ParsingError, parse_document
from common.db import get_engine
from common.job_queue import INGESTION_SUBJECT, ensure_stream, get_nats_connection
from common.log_safety import log_safe
from common.marking_detection import detect_markings, evaluate_markings
from common.models import AuditLogEntry, ClassificationLevel, Document, ReleasabilityValue
from common.object_store import get_object_store
from common.qdrant_store import EMBEDDING_MODEL_KEY
from common.sparse_embedding import embed_sparse
from common.tracing import extract_trace_context, get_tracer
from common.vector_store import ChunkPoint, backend_name, get_store

logger = logging.getLogger("ingestion-worker")

# #134: spans carry ids, counts, and byte sizes only -- never chunk text,
# filenames, or any other corpus content (see common/tracing.py).
tracer = get_tracer("ingestion-worker")

# Generous enough to cover a slow embedding pass over a large document
# without a false-positive redelivery racing the attempt that's already
# in flight -- redelivery is meant for "the worker actually died", not
# "processing is still legitimately running".
ACK_WAIT_SECONDS = 300.0

# Issue #208: a wall-clock budget for one document's parse/chunk/embed. The
# ZIP guard in parsing.py bounds decompression for OOXML formats, but nothing
# bounded *time* -- a crafted PDF within the 50MB upload limit can expand
# enormously inside pdfplumber and hold a worker indefinitely, and no format
# is guarded against simply being pathologically slow.
#
# Deliberately below ACK_WAIT_SECONDS: a document that would outrun the ack
# wait is exactly the case where JetStream starts a second attempt alongside
# the first. Failing at 240s means this worker gives up and marks the document
# `failed` before that happens, rather than two workers grinding on the same
# input. #164's deterministic point ids keep a concurrent replay from
# duplicating vectors; this keeps the concurrency from arising.
#
# Timeout is treated as permanent, not transient: the same input on the same
# hardware will take the same time on redelivery, so retrying burns the budget
# again and the uploader waits longer for the same FR-8 outcome.
PROCESSING_TIMEOUT_SECONDS = float(os.environ.get("PROCESSING_TIMEOUT_SECONDS", "240"))

# Sentinel distinguishing "deliberately purged" from "the object store lost
# it" -- see the FileNotFoundError handler in process_document (#214).
_PURGED_MARKER = "original_object_key is unset (document was purged)"

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

# Issue #138: bound how much text the marking detector scans. Banner/portion
# markings sit in the document body, not gigabytes deep, so scanning the whole
# of a very large document would add latency for no real recall gain.
_ADVISORY_SCAN_LIMIT = 1_000_000


def _apply_tagging_advisory(session: Session, doc: Document, sections: list[ParsedSection]) -> None:
    """Issue #138 Phase 1: compute the advisory marking-mismatch finding, attach
    it to `doc`, and (only if it has findings) add an audit entry -- both persist
    with the caller's next commit.

    Fail-safe by construction: any error here is swallowed and logged, leaving
    doc.tagging_advisory untouched. This is decision-support for the curator, so
    it must never fail, block, or delay ingestion -- FR-11's spillage control
    stays the human curator's job, not this signal's.
    """
    try:
        text = "\n".join(s.text for s in sections)[:_ADVISORY_SCAN_LIMIT]
        levels = session.exec(
            select(ClassificationLevel).where(ClassificationLevel.active == True)  # noqa: E712
        ).all()
        rank_by_value = {lvl.value: lvl.rank for lvl in levels}
        # The configured Releasability vocabulary, so caveat comparison can't
        # false-positive on marking segments that aren't releasability values
        # at all (CUI control markings like SP-CTI) -- see evaluate_markings.
        releasability_values = session.exec(
            select(ReleasabilityValue).where(ReleasabilityValue.active == True)  # noqa: E712
        ).all()
        advisory = evaluate_markings(
            assigned_classification=doc.classification,
            assigned_releasability=doc.releasability,
            detected=detect_markings(text),
            rank_by_value=rank_by_value,
            known_caveats=[r.value for r in releasability_values],
        )
        doc.tagging_advisory = advisory.to_dict()
        if advisory.has_findings:
            logger.info(
                "document %s tagging advisory: under_classified=%s unassigned_caveats=%s",
                doc.id,
                advisory.under_classified,
                advisory.unassigned_caveats,
            )
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.tagging_advisory",
                    target_id=str(doc.id),
                    detail=advisory.to_dict(),
                )
            )
    except Exception:
        logger.exception(
            "tagging advisory failed for document %s; continuing ingestion without it",
            doc.id,
        )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _claim_document(
    session: Session, document_id: uuid.UUID, delivery_attempt: int
) -> tuple[Document | None, bool]:
    """Claim one document under a row lock.

    Returns ``(doc, False)`` when this caller owns processing. ``(None, True)``
    means the message is a safe-to-ack duplicate or references terminal work.
    A JetStream redelivery (attempt > 1) may reclaim ``processing`` after a
    crash; a separate duplicate message (attempt 1) cannot race an active
    worker whose lease is still fresh.
    """
    doc = session.get(Document, document_id, with_for_update=True)
    if doc is None:
        logger.error("document %s not found, acking to drop the message", document_id)
        metrics.jobs_total.labels(outcome="missing").inc()
        return None, True

    # ``embedded`` is a checkpoint, not a terminal state. A process can die
    # after committing it and before the pending_review/audit commit below;
    # the redelivery must then replay the stable-id upsert and finish the
    # transition rather than acknowledge a permanently stranded document.
    if doc.status not in {"queued", "processing", "embedded"}:
        logger.info(
            "document %s is already %s; acknowledging duplicate job",
            document_id,
            doc.status,
        )
        metrics.jobs_total.labels(outcome="duplicate_terminal").inc()
        return None, True

    started_at = getattr(doc, "processing_started_at", None)
    if (
        doc.status in {"processing", "embedded"}
        and delivery_attempt <= 1
        and started_at is not None
        and (_utcnow() - _as_utc(started_at)).total_seconds() < ACK_WAIT_SECONDS
    ):
        logger.info(
            "document %s already has an active processing lease; acknowledging duplicate job",
            document_id,
        )
        metrics.jobs_total.labels(outcome="duplicate_inflight").inc()
        return None, True

    doc.status = "processing"
    doc.processing_started_at = _utcnow()
    doc.updated_at = _utcnow()
    session.add(doc)
    session.commit()
    return doc, False


async def process_document(document_id: uuid.UUID, delivery_attempt: int = 1) -> bool:
    """Returns True for a terminal outcome (success or permanent failure --
    ack the message either way), False for a transient/unexpected error
    (don't ack -- let JetStream redeliver)."""
    started = perf_counter()
    try:
        return await _process_document(document_id, delivery_attempt)
    finally:
        metrics.job_seconds.observe(perf_counter() - started)


async def _process_document(document_id: uuid.UUID, delivery_attempt: int) -> bool:
    with Session(get_engine()) as session:
        doc, terminal = _claim_document(session, document_id, delivery_attempt)
        if terminal:
            return True
        if doc is None:
            # _claim_document only returns (None, False) if its contract is
            # broken -- a real check rather than an assert, because asserts
            # are stripped under `python -O` and bandit flags them (B101).
            # Terminal: nothing to retry if the claim returned nothing.
            logger.error("claim for %s returned no document; dropping", log_safe(document_id))
            return True

        try:
            if doc.original_object_key is None:
                # A document can be purged (common/purge.py) while still
                # queued/processing -- original_object_key is cleared as part
                # of that. Same permanent-failure handling as a missing
                # object-store key below: retrying can't produce a key that
                # was deliberately destroyed.
                raise FileNotFoundError(_PURGED_MARKER)
            # #208: one wall-clock budget for the whole CPU-bound stretch
            # -- fetch, parse, chunk, embed. The vector upsert is left
            # outside it deliberately: a slow Qdrant write is a transient
            # infrastructure failure that should be redelivered, not a
            # pathological document that should be failed permanently.
            async with asyncio.timeout(PROCESSING_TIMEOUT_SECONDS):
                contents = get_object_store().get(doc.original_object_key)
                metrics.document_bytes.observe(len(contents))
                # #134's ingest.process stage spans: attribute values are counts
                # and byte sizes only, never the text they describe.
                with (
                    metrics.stage_seconds.labels(stage="parse").time(),
                    tracer.start_as_current_span("parse") as span,
                ):
                    span.set_attribute("document.bytes", len(contents))
                    # #208: to_thread, not a direct call. parse_document is
                    # synchronous and CPU-bound, so calling it inline blocks the
                    # event loop for its whole duration -- which is both why
                    # /health stops answering during a pathological parse and why
                    # an asyncio timeout around it could never fire (there is no
                    # await point to cancel at). Off the loop, the timeout below
                    # is real and the consumer stays responsive.
                    sections = await asyncio.to_thread(parse_document, doc.filename, contents)
                    span.set_attribute("document.sections", len(sections))
                # Issue #138: advisory only, never blocks -- see _apply_tagging_advisory.
                # Runs on the parsed sections only, before captions are added:
                # the advisory looks for uploader-applied banner/portion
                # markings, and a model-generated caption is not one.
                _apply_tagging_advisory(session, doc, sections)
                # Issue #92: caption embedded images so figure content becomes
                # retrievable. Degrade-on-failure by contract (never raises,
                # never fails the document) and internally bounded well under
                # PROCESSING_TIMEOUT_SECONDS -- see app/captioning.py.
                if captioning_enabled():
                    with (
                        metrics.stage_seconds.labels(stage="caption").time(),
                        tracer.start_as_current_span("caption") as span,
                    ):
                        image_sections = await caption_images(doc.filename, contents)
                        span.set_attribute("document.image_sections", len(image_sections))
                    sections.extend(image_sections)
                with (
                    metrics.stage_seconds.labels(stage="chunk").time(),
                    tracer.start_as_current_span("chunk") as span,
                ):
                    chunks = await asyncio.to_thread(chunk_sections, sections)
                    span.set_attribute("document.chunks", len(chunks))
                if not chunks:
                    raise ParsingError("document contained no extractable text")
                metrics.chunks_produced.observe(len(chunks))

                with (
                    metrics.stage_seconds.labels(stage="embed").time(),
                    tracer.start_as_current_span("embed") as span,
                ):
                    span.set_attribute("document.chunks", len(chunks))
                    dense_vectors = await embed_texts([c.text for c in chunks])
                    sparse_vectors = embed_sparse([c.text for c in chunks])

            points = [
                ChunkPoint(
                    # Stable across retries: an ambiguous acknowledgement or
                    # worker crash may process this document more than once,
                    # and upsert must replace the same point rather than
                    # create duplicate retrievable chunks.
                    id=str(uuid.uuid5(doc.id, f"chunk:{chunk.chunk_index}")),
                    dense=dense,
                    sparse=sparse,
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
            # #160: through the backend seam -- Qdrant by default, Milvus when
            # VECTOR_BACKEND=milvus. One span name either way, backend as an
            # attribute, so the A/B reads side by side in Tempo.
            with (
                metrics.stage_seconds.labels(stage="vector_upsert").time(),
                tracer.start_as_current_span("vector.upsert") as span,
            ):
                span.set_attribute("vector.backend", backend_name())
                span.set_attribute("vector.points", len(points))
                store = get_store()
                store.ensure_ready(dense_size=len(dense_vectors[0]))
                store.upsert(points)

            doc.status = "embedded"
            doc.chunk_count = len(chunks)
            session.add(doc)
            session.commit()

            doc.status = "pending_review"
            doc.processing_started_at = None
            doc.updated_at = _utcnow()
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
            metrics.jobs_total.labels(outcome="succeeded").inc()
            metrics.last_success_timestamp_seconds.set(_utcnow().timestamp())
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
            doc.processing_started_at = None
            doc.updated_at = _utcnow()
            # #214: two cases that look identical to the type system and are
            # very different to the person reading the status.
            #
            # A purge is a deliberate, audited destruction (common/purge.py) --
            # saying so is the whole point, and the message is one this
            # codebase wrote, not a library's. The other case is the object
            # store failing to produce a key we believe exists, and *that*
            # exception carries the filesystem path or S3 key layout, which
            # the uploader has no use for and shouldn't see.
            logger.warning(
                "original object missing for %s: %s: %s",
                log_safe(document_id),
                type(exc).__name__,
                log_safe(exc),
            )
            doc.processing_error = (
                "the original was purged and is no longer retrievable"
                if str(exc) == _PURGED_MARKER
                else "the uploaded original is no longer retrievable"
            )
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
            metrics.jobs_total.labels(outcome="permanent_failure").inc()
            return True
        except (ParsingError, EmbeddingError, TimeoutError) as exc:
            # Permanent failures -- corrupt/unsupported input, the embedding
            # service rejecting this exact request outright, or (#208) the
            # document outrunning PROCESSING_TIMEOUT_SECONDS. Retrying the
            # identical input wouldn't help, so land the document in `failed`
            # and ack rather than let JetStream redeliver it forever.
            #
            # Timeout counts as permanent for the same reason: the same bytes
            # on the same hardware take the same time on the next attempt, so
            # redelivering only spends the budget again and delays the FR-8
            # outcome the uploader is waiting for.
            #
            # Caveat worth stating: cancelling the await does not kill the
            # worker thread parse_document is running in -- Python cannot
            # interrupt a thread. The consumer is freed immediately and the
            # orphaned thread's result is discarded when it eventually
            # finishes. That bounds the *consumer*, not the process's CPU.
            doc.status = "failed"
            doc.processing_started_at = None
            doc.updated_at = _utcnow()
            doc.processing_error = (
                f"processing exceeded {PROCESSING_TIMEOUT_SECONDS:.0f}s"
                if isinstance(exc, TimeoutError)
                else str(exc)
            )
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
            metrics.jobs_total.labels(outcome="permanent_failure").inc()
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
            # Make the claim immediately reclaimable by the scheduled NAK.
            # If the database itself is still unavailable this may fail; the
            # redelivery number (>1) is the independent reclaim signal.
            try:
                doc.status = "queued"
                doc.processing_started_at = None
                doc.updated_at = _utcnow()
                session.add(doc)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception(
                    "could not release processing lease for document %s; "
                    "redelivery will reclaim it",
                    document_id,
                )
            metrics.jobs_total.labels(outcome="transient_failure").inc()
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
            doc.processing_started_at = None
            doc.updated_at = _utcnow()
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
            metrics.jobs_total.labels(outcome="delivery_exhausted").inc()
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
        metrics.jobs_total.labels(outcome="malformed").inc()
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
        attempt = msg.metadata.num_delivered
        metrics.delivery_attempts.observe(attempt)
        terminal = await process_document(document_id, delivery_attempt=attempt)
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
        metrics.consumer_running.set(1)

        while True:
            STATUS.last_poll_at = _utcnow()
            metrics.last_poll_timestamp_seconds.set(STATUS.last_poll_at.timestamp())
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
        metrics.consumer_running.set(0)
