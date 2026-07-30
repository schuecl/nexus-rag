from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://nexus_rag:nexus_rag@postgres:5432/nexus_rag"
)


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(
        DATABASE_URL,
        echo=False,
        # Issue #236: after a Postgres restart every pooled connection is dead,
        # and without a checkout-time liveness check the first query on each one
        # raises OperationalError -- services then serve 500s until manually
        # restarted. pre_ping issues a cheap round trip on checkout and
        # transparently replaces a dead connection.
        pool_pre_ping=True,
        # Retire connections after 30 minutes so anything in front of Postgres
        # (PgBouncer server_idle_timeout, cloud proxy/LB idle limits,
        # idle_session_timeout) can't silently drop one first: common defaults
        # for those sit at 60 minutes or above, and 30 minutes is still far
        # coarser than this stack's request rate, so recycling cost is noise.
        pool_recycle=1800,
    )


# Columns added to an already-shipped table after deployments may have created
# it: create_all() below only creates *missing tables*, never alters existing
# ones, and SQLModel SELECTs every mapped column -- so on an upgrade that
# retains the documents table (the Compose postgres-data volume, Helm's
# external PostgreSQL), a missing column doesn't degrade gracefully, it makes
# every Document query fail with UndefinedColumn (PR #2 review). Until a real
# migration tool (e.g. Alembic) replaces init_db, additive columns are listed
# here and back-filled idempotently by _ensure_columns(). Nullable/defaulted
# columns only -- anything more needs the real tool.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "documents": {
        # Issue #138: advisory marking-mismatch findings (common/models.py).
        "tagging_advisory": "JSON",
        # #164: durable Postgres -> JetStream hand-off and duplicate-safe
        # worker claim. Nullable so existing rows upgrade without a table
        # rewrite.
        "queue_published_at": "TIMESTAMP WITH TIME ZONE",
        "processing_started_at": "TIMESTAMP WITH TIME ZONE",
    },
    "portal_settings": {
        # #248: branding + login popup banner, all defaulted so an existing
        # single-row deployment upgrades without a rewrite.
        "app_name": "VARCHAR DEFAULT ''",
        "logo_url": "VARCHAR DEFAULT ''",
        "login_button_text": "VARCHAR DEFAULT ''",
        "login_popup_title": "VARCHAR DEFAULT ''",
        "login_popup_text": "VARCHAR DEFAULT ''",
        "login_popup_active": "BOOLEAN DEFAULT FALSE",
    },
}


def _ensure_columns(engine) -> None:
    inspector = inspect(engine)
    for table, columns in _ADDITIVE_COLUMNS.items():
        if not inspector.has_table(table):
            continue  # create_all just made it, complete with all columns
        existing = {col["name"] for col in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def init_db() -> None:
    """Create tables if they don't exist, then add any missing additive
    columns (_ADDITIVE_COLUMNS). Fine for a dev skeleton; a real migration
    tool (e.g. Alembic) should replace this before production use."""
    SQLModel.metadata.create_all(get_engine())
    _ensure_columns(get_engine())


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
