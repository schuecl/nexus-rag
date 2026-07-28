"""Recover the durable Postgres -> JetStream hand-off.

Postgres and JetStream cannot share a transaction. A submission therefore
stores a nullable acknowledgement timestamp on the document row. This loop
re-publishes queued rows whose acknowledgement is still null; message-id
deduplication and the worker's status/lease guard make the flow at-least-once
without producing duplicate chunks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime

from nats.js.client import JetStreamContext
from sqlmodel import Session, col, select

from app import metrics
from common.db import get_engine
from common.job_queue import publish_ingestion_job
from common.models import Document

logger = logging.getLogger("ingestion-api.recovery")

RECONCILE_INTERVAL_SECONDS = float(os.environ.get("QUEUE_RECONCILE_INTERVAL_SECONDS", "15"))
RECONCILE_BATCH_SIZE = int(os.environ.get("QUEUE_RECONCILE_BATCH_SIZE", "100"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def mark_published(document_id: uuid.UUID, published_at: datetime | None = None) -> bool:
    """Persist the JetStream acknowledgement when the row is still queued."""
    with Session(get_engine()) as session:
        doc = session.get(Document, document_id)
        if doc is None or doc.status != "queued":
            return False
        doc.queue_published_at = published_at or _utcnow()
        session.add(doc)
        session.commit()
        return True


def _unpublished_batch() -> list[tuple[uuid.UUID, datetime]]:
    with Session(get_engine()) as session:
        docs = session.exec(
            select(Document)
            .where(Document.status == "queued")
            .where(Document.queue_published_at == None)  # noqa: E711
            .order_by(col(Document.created_at))
            .limit(RECONCILE_BATCH_SIZE)
        ).all()
        return [(doc.id, doc.created_at) for doc in docs]


async def reconcile_once(js: JetStreamContext) -> tuple[int, int]:
    """Publish one bounded batch. Returns ``(published, failed)``."""
    pending = _unpublished_batch()
    metrics.queue_reconciliation_pending.set(len(pending))
    if pending:
        oldest = min(_as_utc(created) for _, created in pending)
        metrics.queue_oldest_unpublished_seconds.set(max(0.0, (_utcnow() - oldest).total_seconds()))
    else:
        metrics.queue_oldest_unpublished_seconds.set(0)

    published = failed = 0
    for document_id, _created_at in pending:
        try:
            await publish_ingestion_job(js, str(document_id))
            mark_published(document_id)
            metrics.queue_publish_total.labels(source="reconciler", outcome="acknowledged").inc()
            published += 1
        except Exception:
            metrics.queue_publish_total.labels(source="reconciler", outcome="failed").inc()
            failed += 1
            logger.exception("failed to reconcile queued document %s; will retry", document_id)
    return published, failed


async def reconcile_forever(js: JetStreamContext) -> None:
    while True:
        try:
            await reconcile_once(js)
        except Exception:
            # A database or broker outage must not kill the recovery task;
            # both are exactly the conditions this loop exists to outlive.
            logger.exception("queue reconciliation pass failed; will retry")
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
