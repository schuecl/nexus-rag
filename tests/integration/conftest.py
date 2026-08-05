"""Shared fixtures for tests/integration -- the containerized layer added by
issue #428, distinct from tests/unit/common's mock/in-memory-SQLite tests
(docs/testing.md's two-tree split, issue #113 extended to a third tree here).

This tree needs a live Postgres bootstrapped the same way docker-compose.yml
bootstraps the dev stack's database: infra/postgres/ensure-roles.sh (create
the per-service roles) -> migrate-db-schema (SQLModel create_all()) ->
infra/postgres/grant-service-privileges.sh -> infra/postgres/
apply-service-grants.sh (the actual REVOKE-then-regrant-per-
infra/postgres/grant-matrix.sql pass, issue #278/#309/#319). See
.github/workflows/e2e.yml's `integration` job, which runs exactly that
sequence before this tree, and docs/testing.md's "Containerized integration
layer" section for how to reproduce it locally.

Deliberately NOT reachable from tests/unit or tests/e2e's default `pytest
tests/unit tests/e2e` invocation (pytest.ini's testpaths lists `tests`, but
ci.yml's unit job names the two subtrees explicitly) -- a plain local
`pytest` run that happens to also collect this directory gets a clean skip
(see `_require_live_postgres` below) rather than a confusing connection
error, since most local runs have no Postgres listening on localhost:5432.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# host, port, db name: shared across every role, so read once. Role
# credentials differ per role -- see _ROLE_ENV below, one entry per
# infra/postgres/grant-matrix.sql grantee plus the bootstrap superuser
# (needed to set up/tear down probe rows; the superuser itself is never the
# thing under test).
_HOST = os.environ.get("POSTGRES_HOST", "localhost")
_PORT = os.environ.get("POSTGRES_PORT", "5432")
_DB = os.environ.get("POSTGRES_DB", "nexus_rag")

# role -> (user env var, password env var, dev-stack default user, dev-stack
# default password), matching .env.example / docker-compose.yml's postgres
# service exactly, so this tree needs no config of its own beyond what the
# dev stack (or e2e.yml's `integration` job) already exports.
_ROLE_ENV: dict[str, tuple[str, str, str, str]] = {
    "bootstrap": ("POSTGRES_USER", "POSTGRES_PASSWORD", "nexus_rag", "nexus_rag"),
    "ingestion-api": (
        "INGESTION_API_DB_USER",
        "INGESTION_API_DB_PASSWORD",
        "nexus_rag_ingestion_api",
        "nexus_rag_ingestion_api",
    ),
    "ingestion-worker": (
        "INGESTION_WORKER_DB_USER",
        "INGESTION_WORKER_DB_PASSWORD",
        "nexus_rag_ingestion_worker",
        "nexus_rag_ingestion_worker",
    ),
    "orchestration-mcp": (
        "ORCHESTRATION_MCP_DB_USER",
        "ORCHESTRATION_MCP_DB_PASSWORD",
        "nexus_rag_orchestration_mcp",
        "nexus_rag_orchestration_mcp",
    ),
    "audit-reporting": (
        "AUDIT_REPORTING_DB_USER",
        "AUDIT_REPORTING_DB_PASSWORD",
        "nexus_rag_audit_reporting",
        "nexus_rag_audit_reporting",
    ),
}


def _pg_url(role: str) -> str:
    user_env, password_env, default_user, default_password = _ROLE_ENV[role]
    user = os.environ.get(user_env, default_user)
    password = os.environ.get(password_env, default_password)
    return f"postgresql+psycopg://{user}:{password}@{_HOST}:{_PORT}/{_DB}"


@pytest.fixture(scope="session")
def pg_role_url() -> Callable[[str], str]:
    """Callable[[role], DATABASE_URL] for any role in _ROLE_ENV above."""
    return _pg_url


@pytest.fixture(scope="session", autouse=True)
def _require_live_postgres(pg_role_url: Callable[[str], str]) -> None:
    try:
        engine = create_engine(pg_role_url("bootstrap"), isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:
        pytest.skip(
            "tests/integration needs a live Postgres, bootstrapped per "
            "docs/testing.md's 'Containerized integration layer' section "
            f"(connecting to {pg_role_url('bootstrap')!r} failed: {exc})"
        )


@pytest.fixture(scope="session")
def bootstrap_engine(pg_role_url: Callable[[str], str]) -> Iterator[Engine]:
    """Bootstrap-superuser engine, for setting up/tearing down state around
    a test -- never the credential under test itself (see module docstring)."""
    engine = create_engine(pg_role_url("bootstrap"), isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()
