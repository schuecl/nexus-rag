"""Unit tests for common.db's additive-column back-fill (PR #2 review):
create_all() never alters an existing table, so a column added to a mapped
model after a deployment first created its table must be added by
_ensure_columns() on the next init_db(), or every query against that model
fails with UndefinedColumn.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlmodel import create_engine

import common.db as db
from common.db import _ensure_columns


def _columns(engine, table: str) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns(table)}


def test_adds_missing_column_to_preexisting_table():
    engine = create_engine("sqlite:///:memory:")
    # A documents table from before the tagging_advisory column existed.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY)"))
    assert not {
        "tagging_advisory",
        "queue_published_at",
        "processing_started_at",
        "content_sha256",
        "first_approved_at",
    } & _columns(engine, "documents")

    _ensure_columns(engine)

    assert {
        "tagging_advisory",
        "queue_published_at",
        "processing_started_at",
        "content_sha256",
        "first_approved_at",
    } <= _columns(engine, "documents")
    engine.dispose()


def test_idempotent_when_column_already_present():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY, tagging_advisory JSON)"))

    # Must not raise (a second ADD COLUMN would) and must change nothing.
    _ensure_columns(engine)
    _ensure_columns(engine)

    assert "tagging_advisory" in _columns(engine, "documents")
    engine.dispose()


def test_missing_table_is_skipped():
    # Fresh database where create_all hasn't run: nothing to alter, no error --
    # create_all will create the table complete with all mapped columns.
    engine = create_engine("sqlite:///:memory:")
    _ensure_columns(engine)
    assert not inspect(engine).has_table("documents")
    engine.dispose()


# --- Pool resilience across a database restart (issue #236) -------------------
#
# After a Postgres restart, every connection already in the pool is dead. The
# production engine must detect that at checkout (pool_pre_ping) and replace
# the connection transparently instead of letting the first query raise and
# surface as a 500 until the service is manually restarted.


@pytest.fixture
def fresh_engine(monkeypatch, tmp_path):
    """The production get_engine(), pointed at a throwaway SQLite file so the
    real pool settings are exercised without a live Postgres."""
    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    db.get_engine.cache_clear()
    engine = db.get_engine()
    yield engine
    engine.dispose()
    db.get_engine.cache_clear()


def test_engine_configured_with_pre_ping_and_recycle(fresh_engine):
    assert fresh_engine.pool._pre_ping is True
    # Must sit below connection age limits of anything fronting Postgres
    # (PgBouncer, cloud proxies); the value itself is justified in db.py.
    assert 0 < fresh_engine.pool._recycle <= 3600


def test_stale_pooled_connection_is_replaced_not_raised(fresh_engine):
    # Check a connection out, return it to the pool, then kill the underlying
    # DBAPI connection -- the pool-side equivalent of Postgres restarting.
    conn = fresh_engine.connect()
    conn.execute(text("SELECT 1"))
    dbapi_conn = conn.connection.dbapi_connection
    conn.close()  # back into the pool, still referencing the doomed connection
    dbapi_conn.close()  # dies underneath the pool

    # Next checkout must pre-ping, notice the corpse, and hand back a live
    # replacement -- not raise on first use.
    with fresh_engine.connect() as replacement:
        assert replacement.execute(text("SELECT 1")).scalar() == 1
