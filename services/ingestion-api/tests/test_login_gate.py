"""Issue #246: every page route renders the login landing page instead of its
real content for an anonymous visitor, rather than rendering fully and letting
the first action a logged-out visitor takes fail.

These call the route functions directly (same pattern as
orchestration-mcp/tests/test_query_bounds.py's `_request` helper) rather than
driving the app through TestClient: `main.py`'s lifespan opens a live NATS
JetStream connection this suite has no business standing up just to prove a
template-selection branch.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app import main
from common.claims import UserClaims

PAGE_ROUTES = [
    main.upload_page,
    main.admin_page,
    main.curate_page,
    main.notifications_page,
    main.search_page,
]


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()  # #188: undisposed sqlite3 connections fail an unrelated test at GC time


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            # base.html's `url_for('static', ...)` needs a real app in scope.
            "app": main.app,
        }
    )


class TestAnonymousVisitorSeesLoginPage:
    @pytest.mark.parametrize("route", PAGE_ROUTES)
    def test_every_page_route_renders_login_when_logged_out(self, route, db):
        resp = route(_request(), session=db, current_user=None)

        assert resp.status_code == 200
        assert b"Login via OIDC" in resp.body
        assert b'href="/auth/login"' in resp.body

    def test_upload_form_is_not_present_for_an_anonymous_visitor(self, db):
        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b"Submit a document" not in resp.body

    def test_the_top_nav_is_not_present_for_an_anonymous_visitor(self, db):
        """Issue #246/#248: an anonymous visitor never has anywhere the nav
        would actually lead -- every link on it just bounces back to this
        same login page -- so the header shouldn't render at all."""
        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b"site-header" not in resp.body


class TestAuthenticatedVisitorSeesRealPage:
    @pytest.fixture
    def user(self) -> UserClaims:
        return UserClaims(sub="alice-sub", preferred_username="alice", rag_roles=["rag-ingest"])

    def test_upload_page_renders_the_real_form(self, db, user):
        resp = main.upload_page(_request(), session=db, current_user=user)

        assert resp.status_code == 200
        assert b"Submit a document" in resp.body

    def test_top_nav_is_present_for_a_signed_in_user(self, db, user):
        resp = main.upload_page(_request(), session=db, current_user=user)

        assert b"site-header" in resp.body

    def test_admin_page_renders_for_a_non_admin_signed_in_user(self, db, user):
        """Issue #246 gates on authentication, not on the rag-admin role --
        that authorization stays on /admin/*'s require_admin, unchanged."""
        resp = main.admin_page(_request("/admin"), session=db, current_user=user)

        assert resp.status_code == 200
        assert b"Portal settings" in resp.body

    def test_nav_hides_curate_tab_for_a_user_with_no_curate_role(self, db, user):
        """Issue #249: a user with no rag-curate:<org> role only ever gets a
        403 from every action the curate tab leads to (require_curator in
        app/deps.py), so the tab itself should not be offered."""
        resp = main.upload_page(_request(), session=db, current_user=user)

        assert b"Curation queue" not in resp.body

    def test_nav_shows_curate_tab_for_a_user_with_a_curate_role(self, db):
        curator = UserClaims(
            sub="carol-sub",
            preferred_username="carol",
            rag_roles=["rag-curate:org-a"],
        )
        resp = main.upload_page(_request(), session=db, current_user=curator)

        assert b"Curation queue" in resp.body

    def test_nav_shows_notifications_tab_regardless_of_role(self, db, user):
        """Issue #249 gates the curate tab on role; notifications stays
        available to every signed-in user, since notifications are scoped to
        the recipient (the document's uploader, FR-15) rather than to any
        particular rag_roles grant."""
        resp = main.upload_page(_request(), session=db, current_user=user)

        assert b"Notifications" in resp.body
