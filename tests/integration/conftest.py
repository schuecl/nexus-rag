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
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

# Issue #439: common/qdrant_store.py reads QDRANT_URL into a module-level
# constant the moment it's first imported (`get_store()`'s deferred import,
# triggered the first time a test calls curate.approve()/reject()), so this
# override must land before that happens -- not inside a fixture, which
# would run too late. docker-compose.yml's own default ("http://qdrant:6333")
# is the in-network DNS name; this pytest process runs on the host, so it
# needs the same host-published port Postgres's ci-integration overlay adds
# for itself (127.0.0.1:6333 is already in the base compose file for Qdrant,
# no overlay needed -- see that overlay's header comment).
os.environ["QDRANT_URL"] = os.environ.get("QDRANT_URL_HOST", "http://localhost:6333")
os.environ.setdefault("QDRANT_API_KEY", "dev-qdrant-key")

# Issue #439: this tree's only Postgres-role tests (test_nfr2_...) never
# import a service's `app` package, so this was never needed before. The new
# NFR-13 live-revert test calls ingestion-api's approve()/reject() directly,
# the same way services/ingestion-api/tests/conftest.py does for that
# service's own suite -- this tree must never grow a second service's `app`
# import (see module docstring on the #113 collision this repo works around).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "ingestion-api"))

import pytest
from qdrant_client import QdrantClient
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


# QDRANT_URL/QDRANT_API_KEY are resolved once, above, before any import that
# could read them into a module constant -- reused here rather than
# re-reading os.environ, since QDRANT_URL's raw env var (if a caller set one)
# is the in-network name, not the host-reachable one this fixture needs.
_QDRANT_URL = os.environ["QDRANT_URL"]
_QDRANT_API_KEY = os.environ["QDRANT_API_KEY"]


@pytest.fixture
def qdrant_client() -> Iterator[QdrantClient]:
    """Function-scoped, unlike `_require_live_postgres` above -- deliberately
    NOT session-scoped-autouse: only tests that actually request this
    fixture need live Qdrant (test_nfr2_audit_log_append_only doesn't, and
    must keep skipping on Postgres alone). A session-scoped skip here would
    cache against whichever test resolves it first and wrongly apply to
    every other test in the session regardless of whether *it* needs Qdrant
    (caught by test_nfr2 spuriously skipping when Qdrant alone was down)."""
    try:
        # check_compatibility=False: its default-True background thread logs
        # its own connection failure asynchronously, after this try/except
        # has already handled it via the synchronous get_collections() call
        # below -- with pytest's filterwarnings=error, that stray thread
        # exception turned into a hard test error instead of the clean skip
        # this fixture already produces (reproduced with Qdrant stopped).
        client = QdrantClient(url=_QDRANT_URL, api_key=_QDRANT_API_KEY, check_compatibility=False)
        client.get_collections()
    except Exception as exc:
        pytest.skip(
            "this test needs a live Qdrant, reachable at "
            f"{_QDRANT_URL!r} with QDRANT_API_KEY set (docker compose up -d qdrant "
            f"from the dev stack already exposes this) -- connecting failed: {exc}"
        )
    yield client
    client.close()
