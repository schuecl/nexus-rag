"""Issue #307 Phase 2: the ingestion worker's glue around the kNN precedent
advisory. `find_similar_approved` itself (the Qdrant/Milvus backend query) is
unit-tested in tests/unit/common/test_qdrant_backend_fanout.py; this covers
what the worker does with its results -- fold them into `doc.tagging_advisory`
alongside Phase 1's marking-mismatch finding, audit-log a genuine disagreement,
and, above all, never let any of that break ingestion (it is decision-support,
not a gate), same posture as tests/test_tagging_advisory.py's Phase 1 tests.
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

import app.processing as processing_module
from app.processing import _apply_precedent_advisory
from common.models import AuditLogEntry, ClassificationLevel, Document
from common.vector_store import Hit

RANKS = [("UNCLASSIFIED", 1), ("CUI", 2), ("SECRET", 3)]


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    for value, rank in RANKS:
        session.add(ClassificationLevel(value=value, rank=rank))
    session.commit()
    return session


def _make_doc(classification: str, releasability: list[str] | None = None) -> Document:
    return Document(
        filename="report.txt",
        uploader_sub="user-1",
        uploader_username="alice",
        owner_org="USAREUR-AF",
        classification=classification,
        releasability=releasability or ["NONE"],
        access_scope=["ALL_AUTHENTICATED"],
        source_originator="USAREUR-AF",
        doc_type="report",
    )


def _hit(classification: str, releasability: list[str] | None = None) -> Hit:
    return Hit(
        id="chunk-1",
        score=0.9,
        payload={"classification": classification, "releasability": releasability or []},
    )


def _audit_count(session: Session) -> int:
    return len(
        session.exec(
            select(AuditLogEntry).where(AuditLogEntry.action == "document.tagging_advisory")
        ).all()
    )


class _FakeStore:
    def __init__(self, hits=None, raises=None):
        self._hits = hits or []
        self._raises = raises

    def find_similar_approved(self, *, dense, limit, exclude_document_id=None):
        if self._raises is not None:
            raise self._raises
        return self._hits[:limit]


def _apply(monkeypatch, session, doc, store, dense_vectors=None):
    dense_vectors = dense_vectors or [[1.0, 0.0], [0.0, 1.0]]
    monkeypatch.setattr(processing_module, "get_store", lambda: store)
    _apply_precedent_advisory(session, doc, dense_vectors)


def test_higher_precedent_classification_is_flagged_and_audited(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    store = _FakeStore(hits=[_hit("SECRET", ["NATO"])] * 4 + [_hit("CUI")])

    _apply(monkeypatch, session, doc, store)

    precedent = doc.tagging_advisory["precedent"]
    assert precedent["disagrees_with_assigned"] is True
    assert precedent["top_classification"] == "SECRET"
    assert precedent["top_classification_count"] == 4
    assert precedent["similar_count"] == 5
    assert precedent["top_releasability"] == ["NATO"]
    assert _audit_count(session) == 1


def test_precedent_matching_assigned_tag_is_not_flagged(monkeypatch):
    session = _make_session()
    doc = _make_doc("SECRET")
    store = _FakeStore(hits=[_hit("SECRET")] * 3)

    _apply(monkeypatch, session, doc, store)

    precedent = doc.tagging_advisory["precedent"]
    assert precedent["disagrees_with_assigned"] is False
    assert _audit_count(session) == 0


def test_lower_precedent_classification_is_not_flagged(monkeypatch):
    # Precedent tagged *below* the assigned classification isn't an
    # under-classification signal -- disagrees_with_assigned only fires when
    # precedent reads higher, mirroring Phase 1's under_classified semantics.
    session = _make_session()
    doc = _make_doc("SECRET")
    store = _FakeStore(hits=[_hit("UNCLASSIFIED")] * 3)

    _apply(monkeypatch, session, doc, store)

    precedent = doc.tagging_advisory["precedent"]
    assert precedent["disagrees_with_assigned"] is False
    assert _audit_count(session) == 0


def test_no_hits_leaves_tagging_advisory_untouched(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    doc.tagging_advisory = {"under_classified": False}
    store = _FakeStore(hits=[])

    _apply(monkeypatch, session, doc, store)

    assert doc.tagging_advisory == {"under_classified": False}
    assert _audit_count(session) == 0


def test_merges_with_existing_phase1_advisory_without_clobbering_it(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")
    doc.tagging_advisory = {"under_classified": True, "detected_classification": "SECRET"}
    store = _FakeStore(hits=[_hit("CUI")] * 3)

    _apply(monkeypatch, session, doc, store)

    assert doc.tagging_advisory["under_classified"] is True
    assert doc.tagging_advisory["detected_classification"] == "SECRET"
    assert "precedent" in doc.tagging_advisory


def test_store_unavailable_is_swallowed_and_leaves_advisory_untouched(monkeypatch):
    session = _make_session()
    doc = _make_doc("CUI")

    store = _FakeStore(raises=RuntimeError("qdrant unreachable"))

    _apply(monkeypatch, session, doc, store)

    assert doc.tagging_advisory is None
    assert _audit_count(session) == 0


def test_unranked_classification_is_not_flagged(monkeypatch):
    # A precedent classification absent from the configured ClassificationLevel
    # list can't be ranked against the assigned tag -- fail closed on the
    # comparison (no finding), not on the whole advisory.
    session = _make_session()
    doc = _make_doc("CUI")
    store = _FakeStore(hits=[_hit("UNKNOWN-LEVEL")] * 3)

    _apply(monkeypatch, session, doc, store)

    precedent = doc.tagging_advisory["precedent"]
    assert precedent["disagrees_with_assigned"] is False
