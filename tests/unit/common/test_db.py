"""Unit tests for common.db's additive-column back-fill (PR #2 review):
create_all() never alters an existing table, so a column added to a mapped
model after a deployment first created its table must be added by
_ensure_columns() on the next init_db(), or every query against that model
fails with UndefinedColumn.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlmodel import create_engine

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
    } & _columns(engine, "documents")

    _ensure_columns(engine)

    assert {
        "tagging_advisory",
        "queue_published_at",
        "processing_started_at",
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
