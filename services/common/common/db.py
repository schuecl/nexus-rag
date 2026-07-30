from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://nexus_rag:nexus_rag@postgres:5432/nexus_rag"
)


# Issue #236: a pooled connection outlives the server it points at.
#
# Without pool_pre_ping, SQLAlchemy hands out a connection from the pool
# without checking it is alive. Any Postgres restart -- patching, failover, a
# managed-Postgres maintenance window, a plain `docker compose restart
# postgres` -- therefore leaves every service holding dead connections and
# serving 500s until someone restarts the service itself.
#
# That is worse than it sounds, because /health does not touch the database.
# The service reports healthy, an orchestrator sees no reason to restart it,
# and an operator sees green while every real request fails. The failure is
# silent exactly where it should be loud.
#
# pool_pre_ping costs one cheap round trip per checkout, which is nothing next
# to the embedding call and vector search already on the retrieval path.
#
# pool_recycle is the other half: pre_ping catches a connection that is already
# dead, while recycle retires one before whatever sits in front of Postgres
# (PgBouncer, a cloud proxy, idle_session_timeout) drops it silently. 1800s is
# below the common defaults for all three and well below Postgres's own
# (disabled by default), so it is a safe floor rather than a tuned value.
#
# Tunable because 1800 is a guess about *other* systems, and an air-gapped
# deployment sits behind whatever intermediary the environment provides. An
# operator whose proxy is more aggressive can lower it; -1 disables recycling
# (SQLAlchemy's own sentinel) for an environment that has no such intermediary.
DEFAULT_POOL_RECYCLE_SECONDS = 1800


def _pool_recycle_seconds() -> int:
    """Read DB_POOL_RECYCLE_SECONDS, falling back loudly on a bad value.

    Deliberately does not raise. This is read at import time, so a typo -- or
    the far more likely `DB_POOL_RECYCLE_SECONDS=` that an unset key in a .env
    file produces, which Compose passes through as an empty string -- would
    otherwise crash every service at startup with a traceback several layers
    from its cause. That is the #221 failure mode: the symptom surfaces nowhere
    near the mistake, and in an air-gapped environment nobody can afford to
    debug it.

    A wrong recycle interval costs round trips; pool_pre_ping still guarantees a
    dead connection is replaced at checkout. So degrading to the default and
    saying so is strictly better than refusing to start.
    """
    raw = os.environ.get("DB_POOL_RECYCLE_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_POOL_RECYCLE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "DB_POOL_RECYCLE_SECONDS=%r is not an integer; using the default %ds",
            raw,
            DEFAULT_POOL_RECYCLE_SECONDS,
        )
        return DEFAULT_POOL_RECYCLE_SECONDS
    # -1 is SQLAlchemy's "never recycle". 0 would retire every connection on
    # checkout, which silently turns pooling off and is never what anyone means.
    if value == 0 or value < -1:
        logger.warning(
            "DB_POOL_RECYCLE_SECONDS=%d is not a usable interval (0 recycles every "
            "connection, negative values other than -1 are undefined); using the "
            "default %ds. Use -1 to disable recycling.",
            value,
            DEFAULT_POOL_RECYCLE_SECONDS,
        )
        return DEFAULT_POOL_RECYCLE_SECONDS
    return value


POOL_RECYCLE_SECONDS = _pool_recycle_seconds()


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
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
        # #285: content integrity digest, nullable so pre-existing rows
        # (uploaded before this column existed) upgrade without a rewrite --
        # ingestion-worker's re-verification check is skipped for those.
        "content_sha256": "VARCHAR(64)",
        # #286: durable first-approval timestamp for the chat-plane purge
        # signal; survives the #268 pending_review demotion that wipes
        # reviewed_at. Nullable: pre-existing rows upgrade without a rewrite,
        # and purge.py treats null as "trust status_before_purge instead".
        "first_approved_at": "TIMESTAMP WITH TIME ZONE",
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
