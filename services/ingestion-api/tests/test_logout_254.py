"""Issue #254: logging out did not end the browser's Keycloak SSO session, so
pressing "Login via OIDC" again silently re-authenticated the same user
without prompting for credentials.

Root cause: base.html's `logout()` POSTed to `/auth/logout` with `fetch()`
and let fetch follow the resulting 303 redirect through to Keycloak's
end_session_endpoint itself. That hop is not a top-level navigation, and
Keycloak's own SSO session cookie is SameSite=Lax -- browsers only attach a
Lax cookie to a top-level navigation, never to a fetch-driven request
(redirected or not). So Keycloak never saw the logout and the SSO session
outlived it.

The fix moves the browser hop to Keycloak out of fetch() entirely: this route
now returns the target as JSON instead of redirecting to it, and base.html
does a real `window.location` navigation with it (a top-level GET, same as
following a link) so the SSO cookie rides along as it should.

These tests call `auth.logout` directly (same pattern as
test_session_lifecycle.py) rather than through TestClient/full app lifespan.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app.deps import CSRF_COOKIE, SESSION_COOKIE
from app.routes import auth
from common.models import UserSession


def _user_session(**overrides: object) -> UserSession:
    defaults: dict[str, object] = {
        "id": "sid",
        "access_token": "t",
        "expires_at": datetime.now(UTC) + timedelta(minutes=15),
    }
    defaults.update(overrides)
    return UserSession(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()  # #188: undisposed sqlite3 connections fail an unrelated test at GC time


def _request(cookie_header: str = "") -> Request:
    headers = [(b"cookie", cookie_header.encode())] if cookie_header else []
    return Request({"type": "http", "method": "POST", "path": "/auth/logout", "headers": headers})


def _set_cookie_values(resp) -> dict[str, str]:
    """Parses the `key=value` out of each Set-Cookie header, ignoring
    attributes (Path, Max-Age, etc.) -- good enough to see what a cookie was
    set (or cleared) to."""
    values = {}
    for raw in resp.headers.getlist("set-cookie"):
        pair = raw.split(";", 1)[0]
        key, _, value = pair.partition("=")
        values[key] = value
    return values


class TestLogoutReturnsTheTargetRatherThanRedirectingToIt:
    def test_a_session_with_an_id_token_gets_the_keycloak_end_session_url(self, db):
        db.add(_user_session(id_token="the-id-token"))
        db.commit()

        resp = auth.logout(_request(f"{SESSION_COOKIE}=sid"), db=db, _csrf=None)

        assert resp.status_code == 200
        body = json.loads(bytes(resp.body))
        assert "protocol/openid-connect/logout" in body["redirect"]
        assert "id_token_hint=the-id-token" in body["redirect"]

    def test_a_session_without_an_id_token_falls_back_to_local_target(self, db):
        db.add(_user_session(id_token=None))
        db.commit()

        resp = auth.logout(_request(f"{SESSION_COOKIE}=sid"), db=db, _csrf=None)

        assert json.loads(bytes(resp.body)) == {"redirect": "/"}

    def test_no_session_cookie_falls_back_to_local_target(self, db):
        resp = auth.logout(_request(), db=db, _csrf=None)

        assert json.loads(bytes(resp.body)) == {"redirect": "/"}

    def test_the_response_is_not_a_redirect(self, db):
        """The regression itself: a 303 here is what let fetch() swallow the
        hop to Keycloak instead of the browser navigating there for real."""
        resp = auth.logout(_request(), db=db, _csrf=None)

        assert resp.status_code == 200
        assert "location" not in resp.headers


class TestLogoutStillTearsDownLocalState:
    def test_the_session_row_is_deleted(self, db):
        db.add(_user_session(id_token="tok"))
        db.commit()

        auth.logout(_request(f"{SESSION_COOKIE}=sid"), db=db, _csrf=None)

        assert db.get(UserSession, "sid") is None

    def test_the_session_and_csrf_cookies_are_cleared(self, db):
        db.add(_user_session(id_token="tok"))
        db.commit()

        resp = auth.logout(_request(f"{SESSION_COOKIE}=sid"), db=db, _csrf=None)

        cookies = _set_cookie_values(resp)
        assert cookies[SESSION_COOKIE] == '""'
        assert cookies[CSRF_COOKIE] == '""'

    def test_an_unknown_session_id_does_not_raise(self, db):
        resp = auth.logout(_request(f"{SESSION_COOKIE}=does-not-exist"), db=db, _csrf=None)

        assert json.loads(bytes(resp.body)) == {"redirect": "/"}
