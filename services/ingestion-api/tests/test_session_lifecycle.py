"""Coverage for issue #108: oauth_states and user_sessions have bounded
lifetimes, and the rows that back them get reaped.

Both properties were previously only described, not implemented -- the
OAuthState docstring called abandoned rows "low-volume enough not to need a
cleanup job", /callback's error string said "unknown or expired OAuth state"
with nothing checking expiry, and SESSION_LIFETIME bounded only the cookie's
max_age while the server-side row stayed renewable indefinitely.

These run against an in-memory SQLite engine rather than Postgres: the
lifetime rules are plain datetime arithmetic over columns that already
existed (no schema change was needed for this fix), so nothing here depends
on the real dialect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import deps
from app.routes import auth
from common.models import OAuthState, UserSession


def _utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    # Disposing is not optional housekeeping: pytest 9 turns the
    # ResourceWarning an un-disposed sqlite3 connection raises at GC time into
    # a test error, and because GC runs whenever it likes, the error lands on
    # whichever unrelated test is executing at the time (#188).
    engine.dispose()


def _session_row(*, age: timedelta) -> UserSession:
    """A session created `age` ago whose access token is still fresh -- so
    any rejection is attributable to the absolute lifetime, not to
    expires_at."""
    return UserSession(
        id="sid",
        access_token="token",
        refresh_token="refresh",
        expires_at=_utcnow() + timedelta(minutes=15),
        created_at=_utcnow() - age,
    )


class TestAbsoluteSessionLifetime:
    def test_a_fresh_session_is_not_expired(self):
        assert deps.session_expired(_session_row(age=timedelta(minutes=5))) is False

    def test_a_session_past_its_lifetime_is_expired(self):
        row = _session_row(age=deps.SESSION_LIFETIME + timedelta(minutes=1))

        assert deps.session_expired(row) is True

    def test_expiry_is_measured_from_created_at_not_expires_at(self):
        """The distinction the fix turns on: expires_at is the access
        token's expiry and _refresh_session renews it. created_at is the one
        nothing renews."""
        row = _session_row(age=deps.SESSION_LIFETIME + timedelta(hours=100))
        row.expires_at = _utcnow() + timedelta(days=365)

        assert deps.session_expired(row) is True

    def test_naive_created_at_is_treated_as_utc(self):
        """sqlite (and any non-timezone(True) column) round-trips tz-aware
        datetimes as naive -- the comparison must not raise on that."""
        row = _session_row(age=timedelta(minutes=5))
        row.created_at = row.created_at.replace(tzinfo=None)

        assert deps.session_expired(row) is False

    def test_expired_session_is_deleted_and_never_refreshed(self, db, monkeypatch):
        """The ordering that matters: an expired session must not reach
        _refresh_session, or the absolute lifetime would be renewable."""
        refreshed = False

        def _refresh(*_a):
            nonlocal refreshed
            refreshed = True
            return None

        monkeypatch.setattr(deps, "_refresh_session", _refresh)
        row = _session_row(age=deps.SESSION_LIFETIME + timedelta(minutes=1))
        db.add(row)
        db.commit()

        assert deps._claims_from_session(db, row) is None
        assert refreshed is False, "an expired session must not be renewable"
        assert db.get(UserSession, "sid") is None, "the row should be dropped"


class TestOAuthStateTtl:
    def test_ttl_matches_the_state_cookie_max_age(self):
        """Past the cookie's max_age the browser stops sending the value the
        state must match, so a longer row TTL would be dead weight."""
        assert timedelta(seconds=600) == auth.OAUTH_STATE_TTL

    def test_purge_removes_expired_states_but_keeps_live_ones(self, db):
        now = _utcnow()
        db.add(OAuthState(id="old", code_verifier="v", created_at=now - timedelta(hours=1)))
        db.add(OAuthState(id="new", code_verifier="v", created_at=now))
        db.commit()

        auth._purge_expired(db)

        assert [r.id for r in db.exec(select(OAuthState)).all()] == ["new"]

    def test_purge_removes_sessions_past_the_absolute_lifetime(self, db):
        now = _utcnow()
        db.add(_session_row(age=deps.SESSION_LIFETIME + timedelta(hours=1)))
        live = UserSession(
            id="live", access_token="t", expires_at=now + timedelta(minutes=5), created_at=now
        )
        db.add(live)
        db.commit()

        auth._purge_expired(db)

        assert [r.id for r in db.exec(select(UserSession)).all()] == ["live"]

    def test_purge_is_safe_on_empty_tables(self, db):
        auth._purge_expired(db)

        assert db.exec(select(OAuthState)).all() == []
