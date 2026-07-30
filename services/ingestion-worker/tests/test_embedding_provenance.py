"""Coverage for issue #122, write side: every chunk records which embedding
model produced its vector.

The read side (detecting a mismatch at query time) lives in
services/orchestration-mcp/tests/test_embedding_provenance.py. This half is
what makes that possible -- without the stamp there is nothing to compare.

Worth testing explicitly rather than leaning on the compose e2e: an extra or
missing payload key is invisible there, because Qdrant accepts any payload
shape and retrieval keeps working. A silently-absent stamp would leave
mismatch detection permanently inactive with nothing failing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app import processing
from app.chunking import Chunk
from common.qdrant_store import EMBEDDING_MODEL_KEY


class _FakeStore:
    def get(self, _key):
        return b"bytes"


class _Doc:
    def __init__(self):
        self.id = uuid.uuid4()
        self.filename = "report.pdf"
        self.uploader_sub = "alice-sub"
        self.uploader_username = "alice-ingest"
        self.doc_type = "report"
        self.source_originator = "USAREUR-AF G2"
        self.classification = "CUI"
        self.releasability = ["FVEY"]
        self.access_scope = ["USAREUR-AF"]
        self.original_object_key = "documents/x/original"
        self.status = "queued"
        self.chunk_count = 0
        self.processing_error = None


@pytest.fixture
def captured(monkeypatch):
    """Drive process_document far enough to capture the points it upserts,
    with every external dependency stubbed."""
    doc = _Doc()
    points: list = []

    class _Session:
        def get(self, _model, _id, **_kwargs):
            return doc

        def add(self, _obj):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(processing, "Session", lambda _engine: _Session())
    monkeypatch.setattr(processing, "get_engine", object)
    monkeypatch.setattr(processing, "get_object_store", _FakeStore)
    monkeypatch.setattr(processing, "parse_document", lambda _f, _c: ["section"])
    monkeypatch.setattr(
        processing,
        "chunk_sections",
        lambda _s: [Chunk(text="body", chunk_index=0, heading="H", page_or_slide=1)],
    )

    async def _embed(_texts):
        return [[0.1, 0.2]]

    monkeypatch.setattr(processing, "embed_texts", _embed)
    monkeypatch.setattr(processing, "embed_sparse", lambda _t: [object()])

    # #160: the pipeline writes through the vector-store seam now.
    class _Store:
        def ensure_ready(self, dense_size, classification):
            return None

        def upsert(self, pts):
            points.extend(pts)

    monkeypatch.setattr(processing, "get_store", _Store)
    monkeypatch.setattr(processing, "AuditLogEntry", lambda **_kw: object())

    return doc, points


class TestChunkProvenance:
    async def test_chunk_id_is_stable_across_retries(self, captured):
        doc, points = captured

        await processing.process_document(doc.id)

        assert points[0].id == str(uuid.uuid5(doc.id, "chunk:0"))

    async def test_chunk_records_the_embedding_model(self, captured):
        doc, points = captured

        terminal = await processing.process_document(doc.id)

        assert terminal is True
        assert points, "sanity: a point was upserted"
        assert points[0].payload[EMBEDDING_MODEL_KEY] == processing.EMBEDDING_MODEL

    async def test_chunk_records_when_it_was_embedded(self, captured):
        doc, points = captured

        await processing.process_document(doc.id)

        # ISO-8601, parseable -- this is provenance, not a display string.
        assert datetime.fromisoformat(points[0].payload["embedded_at"])

    async def test_existing_payload_fields_are_untouched(self, captured):
        """The stamp is additive; FR-26 filtering and FR-27 citation both read
        from this payload and must not regress."""
        doc, points = captured

        await processing.process_document(doc.id)

        payload = points[0].payload
        for key in (
            "document_id",
            "chunk_index",
            "text",
            "heading",
            "page_or_slide",
            "content_type",
            "filename",
            "classification",
            "releasability",
            "access_scope",
            "status",
        ):
            assert key in payload, f"{key} went missing from the chunk payload"
        assert payload["status"] == "pending_review", "must stay excluded from retrieval"
