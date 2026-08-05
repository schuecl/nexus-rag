"""Issue #428 (NFR-2 slice): audit_log append-only enforcement, verified at
the database level against a live Postgres instead of assumed from
application code never issuing the forbidden statement.

NFR-2 (REQUIREMENTS.md): "The application's own database credentials must
not carry update or delete privileges on the audit log table." Every
application role gets INSERT only; a dedicated audit-reporting role gets
SELECT only (issue #309) -- infra/postgres/grant-matrix.sql is the single
source of truth this test pins itself against, applied live by
infra/postgres/apply-service-grants.sh.

This is exactly the property tests/unit/common/test_purge.py and friends
cannot exercise: those run against in-memory SQLite, which has no privilege
system to enforce, so a regression here (a grant script that silently starts
granting UPDATE/DELETE) would pass every existing test and only be caught by
a live connection actually attempting the forbidden statement -- which is
what this file does. Confirmed to actually catch that regression: manually
re-granting SELECT to the ingestion-api role and rerunning
test_select_denied[ingestion-api] against a live stack turns it red with
"DID NOT RAISE"; revoking it again turns the suite back green.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError

from common.models import AuditLogEntry

# infra/postgres/grant-matrix.sql: every one of these gets INSERT only.
_APP_ROLES = ["ingestion-api", "ingestion-worker", "orchestration-mcp"]

_PROBE_ACTOR_SUB = "integration-test-nfr2-probe"


def _probe_row(action: str) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "actor_sub": _PROBE_ACTOR_SUB,
        "actor_username": "integration-test",
        "action": action,
        "target_id": None,
        "detail": {},
    }


@pytest.fixture
def role_engine(pg_role_url: Callable[[str], str]) -> Iterator[Callable[[str], Engine]]:
    """Callable[[role], Engine], AUTOCOMMIT so each statement below succeeds
    or fails on its own -- Postgres aborts the rest of a transaction after
    its first error, which would make every statement after the first
    expected denial fail with "current transaction is aborted" instead of
    actually exercising its own grant. Disposed in teardown regardless of
    test outcome, so a failed pytest.raises() (an assertion, not a Postgres
    error) can't leak the connection under it -- unlike a bare
    engine.dispose() at the end of the test body, which never runs when the
    test fails first."""
    engines: list[Engine] = []

    def _make(role: str) -> Engine:
        engine = create_engine(pg_role_url(role), isolation_level="AUTOCOMMIT")
        engines.append(engine)
        return engine

    yield _make
    for engine in engines:
        engine.dispose()


@pytest.fixture(autouse=True)
def _cleanup_probe_rows(bootstrap_engine: Engine) -> Iterator[None]:
    yield
    with bootstrap_engine.connect() as conn:
        conn.execute(delete(AuditLogEntry).where(AuditLogEntry.actor_sub == _PROBE_ACTOR_SUB))


class TestApplicationRolesAreInsertOnly:
    @pytest.mark.parametrize("role", _APP_ROLES)
    def test_insert_succeeds(self, role: str, role_engine: Callable[[str], Engine]) -> None:
        with role_engine(role).connect() as conn:
            conn.execute(insert(AuditLogEntry).values(**_probe_row(f"{role}.insert-allowed")))

    @pytest.mark.parametrize("role", _APP_ROLES)
    def test_select_denied(self, role: str, role_engine: Callable[[str], Engine]) -> None:
        with (
            role_engine(role).connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(select(AuditLogEntry).limit(1))

    @pytest.mark.parametrize("role", _APP_ROLES)
    def test_update_denied(self, role: str, role_engine: Callable[[str], Engine]) -> None:
        with (
            role_engine(role).connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(update(AuditLogEntry).values(action="tampered"))

    @pytest.mark.parametrize("role", _APP_ROLES)
    def test_delete_denied(self, role: str, role_engine: Callable[[str], Engine]) -> None:
        with (
            role_engine(role).connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(delete(AuditLogEntry))


class TestAuditReportingRoleIsSelectOnly:
    """The mirror image of the app roles above -- issue #309's dedicated,
    offline reader (scripts/calibrate_tagging_advisory.py's own docstring
    names it) gets SELECT and nothing else, not even INSERT."""

    def test_select_succeeds(self, role_engine: Callable[[str], Engine]) -> None:
        with role_engine("audit-reporting").connect() as conn:
            conn.execute(select(AuditLogEntry).limit(1))

    def test_insert_denied(self, role_engine: Callable[[str], Engine]) -> None:
        with (
            role_engine("audit-reporting").connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(insert(AuditLogEntry).values(**_probe_row("reporting.insert-denied")))

    def test_update_denied(self, role_engine: Callable[[str], Engine]) -> None:
        with (
            role_engine("audit-reporting").connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(update(AuditLogEntry).values(action="tampered"))

    def test_delete_denied(self, role_engine: Callable[[str], Engine]) -> None:
        with (
            role_engine("audit-reporting").connect() as conn,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            conn.execute(delete(AuditLogEntry))
