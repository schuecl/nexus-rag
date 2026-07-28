"""Regression coverage for the durable Postgres -> JetStream hand-off."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app import recovery


class _JetStream:
    pass


async def test_reconciliation_marks_an_acknowledged_job_published(monkeypatch):
    document_id = uuid.uuid4()
    monkeypatch.setattr(recovery, "_unpublished_batch", lambda: [(document_id, datetime.now(UTC))])
    published: list[str] = []
    marked: list[uuid.UUID] = []

    async def _publish(_js, value):
        published.append(value)

    monkeypatch.setattr(recovery, "publish_ingestion_job", _publish)
    monkeypatch.setattr(
        recovery, "mark_published", lambda value, published_at=None: marked.append(value)
    )

    succeeded, failed = await recovery.reconcile_once(_JetStream())

    assert (succeeded, failed) == (1, 0)
    assert published == [str(document_id)]
    assert marked == [document_id]


async def test_reconciliation_keeps_failed_jobs_for_the_next_pass(monkeypatch):
    first, second = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(recovery, "_unpublished_batch", lambda: [(first, now), (second, now)])
    marked: list[uuid.UUID] = []

    async def _publish(_js, value):
        if value == str(first):
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(recovery, "publish_ingestion_job", _publish)
    monkeypatch.setattr(
        recovery, "mark_published", lambda value, published_at=None: marked.append(value)
    )

    succeeded, failed = await recovery.reconcile_once(_JetStream())

    assert (succeeded, failed) == (1, 1)
    assert marked == [second]
