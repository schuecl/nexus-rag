"""Issue #439 (NFR-13 slice): the revert-on-partial-failure branch in
approve()/reject(), exercised against real Postgres and real Qdrant instead
of the SQLite + hand-rolled-stub combination
services/ingestion-api/tests/test_curate_nfr13_revert.py commits as a
permanent mock-based regression check (see that file's docstring for the
"true multi-container live-environment run" it explicitly leaves out).

Design note (issue #439's decision on how to fault-inject without touching
production code): approve()/reject() already reach Postgres through a plain
injected `session: Session` argument and Qdrant through a plain module-level
`get_store()` call (services/ingestion-api/app/routes/curate.py) -- both are
ordinary Python call sites a test can point at real infrastructure or wrap,
the same way the existing mock test already calls approve()/reject()
directly, bypassing FastAPI's Depends() resolution entirely. This file does
the same, just aimed at live services:

- Postgres: a real Session bound to a live engine (ingestion-api's own DB
  role, via tests/integration/conftest.py's pg_role_url). The commit
  failure is a *real* one -- `session.connection().invalidate()` runs
  before `commit()`, so psycopg/SQLAlchemy raise an actual driver-level
  `sqlalchemy.exc.PendingRollbackError`, not a monkeypatched exception.
- Qdrant: the real QdrantStore backend (common/qdrant_backend.py) against a
  live collection with one real chunk point upserted first, so the revert
  is verified by scrolling the point back out of Qdrant afterward, not just
  by recording that a call happened.

NFR-11 crash-redelivery (the other half of #439) is tracked as a separate,
larger follow-up -- see docs/testing.md's "Containerized integration layer"
section.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, SparseVector
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.exc import PendingRollbackError
from sqlmodel import Session, select

from app.routes import curate
from common.claims import UserClaims
from common.models import AuditLogEntry, ClassificationLevel, Document, Notification
from common.qdrant_store import classification_collection_name
from common.vector_store import ChunkPoint, get_store

CURATOR = UserClaims(
    sub="curator-sub-nfr13-live",
    preferred_username="carol-curator",
    org="USAREUR-AF",
    rag_roles=["rag-curate:USAREUR-AF", "rag-clearance:SECRET", "rag-releasability:NONE"],
)

# Matches services/ingestion-api/app/main.py's DEFAULT_CLASSIFICATIONS --
# duplicated rather than imported, since importing app.main would construct
# the FastAPI app and run its module-level route registration just to reach
# three literals; _seed_classification_levels below mirrors _seed_defaults'
# idempotent "only if the table is empty" behavior exactly.
_DEFAULT_CLASSIFICATIONS = [("UNCLASSIFIED", 0), ("CUI", 1), ("SECRET", 2)]


@pytest.fixture(scope="module")
def pg_engine(pg_role_url: Callable[[str], str]) -> Iterator[Engine]:
    engine = create_engine(pg_role_url("ingestion-api"))
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _seed_classification_levels(pg_engine: Engine) -> None:
    with Session(pg_engine) as session:
        if not session.exec(select(ClassificationLevel)).first():
            for value, rank in _DEFAULT_CLASSIFICATIONS:
                session.add(ClassificationLevel(value=value, rank=rank))
            session.commit()


@pytest.fixture
def session(pg_engine: Engine) -> Iterator[Session]:
    with Session(pg_engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _use_live_qdrant_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """curate.py reaches Qdrant through `common.vector_store.get_store()`,
    which is `@lru_cache`d -- clear it so this test gets a fresh QdrantStore
    built against the QDRANT_URL conftest.py already pointed at the
    host-reachable port, regardless of what any earlier test cached."""
    get_store.cache_clear()
    monkeypatch.setattr(curate, "get_store", get_store)


def _document(**overrides: object) -> Document:
    fields: dict[str, object] = {
        "filename": "report.pdf",
        "uploader_sub": "uploader-sub",
        "uploader_username": "alice-ingest",
        "owner_org": "USAREUR-AF",
        "classification": "CUI",
        "releasability": ["NONE"],
        "access_scope": ["ALL_AUTHENTICATED"],
        "source_originator": "USAREUR-AF",
        "doc_type": "report",
        "status": "pending_review",
    }
    fields.update(overrides)
    return Document(**fields)


def _break_commit_with_a_real_failure(session: Session) -> None:
    """Forces an actual Postgres/psycopg failure at commit time -- the
    connection is invalidated first -- rather than a monkeypatched Python
    exception. Confirmed against a live stack to raise
    sqlalchemy.exc.PendingRollbackError, which approve()/reject()'s `except
    Exception:` catches exactly like any other commit failure."""
    original_commit = session.commit

    def _commit_after_invalidating_connection() -> None:
        session.connection().invalidate()
        original_commit()

    session.commit = _commit_after_invalidating_connection  # type: ignore[method-assign]


@pytest.fixture
def live_chunk(
    qdrant_client: QdrantClient, session: Session
) -> Iterator[Callable[[Document], None]]:
    """Upserts one real chunk point for a document into a live Qdrant
    collection, so update_document_payload's set_payload (and its revert)
    mutate an actual point instead of no-op'ing against an empty filter
    match. Yields a factory rather than pre-building the point, since the
    document (and therefore its id) doesn't exist until the test creates it.
    Tracks every (document_id, classification) it wrote so teardown can
    clean up regardless of which test used it."""
    store = get_store()
    written: list[tuple[str, str]] = []

    def _seed(doc: Document) -> None:
        store.ensure_ready(dense_size=4, classification=doc.classification)
        store.upsert(
            [
                ChunkPoint(
                    id=str(uuid.uuid4()),
                    dense=[0.1, 0.2, 0.3, 0.4],
                    sparse=SparseVector(indices=[0], values=[1.0]),
                    payload={
                        "document_id": str(doc.id),
                        "classification": doc.classification,
                        "status": doc.status,
                        "chunk_index": 0,
                    },
                )
            ]
        )
        written.append((str(doc.id), doc.classification))

    yield _seed

    for document_id, classification in written:
        store.delete_document_chunks(document_id, classification)


def _fetch_chunk_status(
    qdrant_client: QdrantClient, collection: str, document_id: str
) -> str | None:
    points, _ = qdrant_client.scroll(
        collection_name=collection,
        scroll_filter=Filter(
            must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
        ),
        limit=1,
        with_payload=True,
    )
    if not points:
        return None
    return points[0].payload.get("status") if points[0].payload else None


class TestApproveRevertsOnRealCommitFailure:
    def test_real_postgres_failure_reverts_real_qdrant_payload(
        self,
        session: Session,
        qdrant_client: QdrantClient,
        live_chunk: Callable[[Document], None],
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        live_chunk(doc)
        # Captured before the commit breaks -- the session's ORM objects are
        # expired after a failed flush/commit, and reloading their attributes
        # would need the very connection this test is about to invalidate.
        doc_id, classification = doc.id, doc.classification
        _break_commit_with_a_real_failure(session)

        with pytest.raises(PendingRollbackError):
            curate.approve(doc_id, corrections=None, user=CURATOR, session=session, _csrf=None)

        collection = classification_collection_name(classification)
        assert _fetch_chunk_status(qdrant_client, collection, str(doc_id)) == "pending_review"

        # Postgres never durably recorded the approval -- confirmed on a
        # fresh session/connection, not the broken one above.
        with Session(session.get_bind()) as verify_session:
            reloaded = verify_session.get(Document, doc_id)
            assert reloaded is not None
            assert reloaded.status == "pending_review"


class TestRejectRevertsOnRealCommitFailure:
    def test_real_postgres_failure_reverts_real_qdrant_payload(
        self,
        session: Session,
        qdrant_client: QdrantClient,
        live_chunk: Callable[[Document], None],
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        live_chunk(doc)
        doc_id, classification = doc.id, doc.classification
        _break_commit_with_a_real_failure(session)

        with pytest.raises(PendingRollbackError):
            curate.reject(
                doc_id,
                curate.Rejection(reason="not relevant"),
                user=CURATOR,
                session=session,
                _csrf=None,
            )

        collection = classification_collection_name(classification)
        assert _fetch_chunk_status(qdrant_client, collection, str(doc_id)) == "pending_review"


class TestApproveHappyPathAgainstLiveInfra:
    """Baseline: against real Postgres + real Qdrant, a successful approval
    durably lands both -- no revert, no leftover pending_review anywhere."""

    def test_successful_approval_commits_and_does_not_revert(
        self,
        session: Session,
        qdrant_client: QdrantClient,
        live_chunk: Callable[[Document], None],
    ) -> None:
        doc = _document()
        session.add(doc)
        session.commit()
        session.refresh(doc)
        live_chunk(doc)

        result = curate.approve(doc.id, corrections=None, user=CURATOR, session=session, _csrf=None)

        assert result.status == "approved"
        collection = classification_collection_name(doc.classification)
        assert _fetch_chunk_status(qdrant_client, collection, str(doc.id)) == "approved"

        with Session(session.get_bind()) as verify_session:
            reloaded = verify_session.get(Document, doc.id)
            assert reloaded is not None
            assert reloaded.status == "approved"


@pytest.fixture(autouse=True)
def _cleanup_postgres_rows(bootstrap_engine: Engine) -> Iterator[None]:
    """Bootstrap superuser, not the ingestion-api role `pg_engine` gives out
    elsewhere in this file -- infra/postgres/grant-matrix.sql deliberately
    doesn't grant that role DELETE on any of these tables (least privilege),
    the same reason test_nfr2_audit_log_append_only.py's own cleanup uses
    bootstrap_engine instead of a role connection."""
    yield
    with Session(bootstrap_engine) as session:
        session.exec(delete(Notification).where(Notification.recipient_sub == "uploader-sub"))
        session.exec(delete(AuditLogEntry).where(AuditLogEntry.actor_sub == CURATOR.sub))
        session.exec(delete(Document).where(Document.uploader_sub == "uploader-sub"))
        session.commit()
