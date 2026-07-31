"""Issue #303 / FR-33: the in-app knowledge base at GET /kb.

Same test style as test_login_gate.py -- route functions are called directly
against an in-memory SQLite session rather than through TestClient, since
main.py's lifespan opens a live NATS JetStream connection this suite has no
business standing up just to prove a template-rendering/role-gating branch.

Two things are pinned here:
1. /kb follows the same auth-only page-gate convention as every other page
   route (anonymous visitor -> login page, never a 404 or a role check on the
   page itself).
2. Which *articles* render inside the page is derived purely from the
   signed-in user's own UserClaims -- one article per capability role, shown
   only when that role is held, mirroring how base.html already gates nav
   tabs on the same properties.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine
from starlette.requests import Request

from app import main
from common.claims import UserClaims


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _request(path: str = "/kb") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "app": main.app,
        }
    )


class TestAnonymousVisitor:
    def test_kb_page_renders_login_when_logged_out(self, db):
        resp = main.kb_page(_request(), session=db, current_user=None)

        assert resp.status_code == 200
        assert b"Login via OIDC" in resp.body

    def test_nav_icon_is_not_present_for_an_anonymous_visitor(self, db):
        resp = main.upload_page(_request("/"), session=db, current_user=None)

        assert b"site-header" not in resp.body


class TestNavIconVisibility:
    def test_nav_icon_shown_for_any_signed_in_user_regardless_of_role(self, db):
        """Unlike the Curation/Admin tabs, the KB icon isn't gated on a
        specific role -- article visibility is decided inside kb.html
        itself, so every signed-in user gets a way in."""
        user = UserClaims(sub="alice-sub", preferred_username="alice", rag_roles=["rag-ingest"])
        resp = main.upload_page(_request("/"), session=db, current_user=user)

        assert b'href="/kb"' in resp.body
        assert b"Knowledge base" in resp.body


class TestArticleVisibilityByRole:
    def test_ingest_only_user_sees_only_the_ingest_article(self, db):
        user = UserClaims(sub="alice-sub", preferred_username="alice", rag_roles=["rag-ingest"])
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert b"Submitting documents" in resp.body
        assert b"Searching the corpus" not in resp.body
        assert b"Reviewing submissions" not in resp.body
        assert b"Permanent destruction" not in resp.body
        assert b"Portal configuration" not in resp.body

    def test_query_only_user_sees_only_the_query_article(self, db):
        user = UserClaims(sub="bob-sub", preferred_username="bob", rag_roles=["rag-query"])
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert b"Searching the corpus" in resp.body
        assert b"Submitting documents" not in resp.body

    def test_curator_sees_only_the_curate_article(self, db):
        user = UserClaims(
            sub="carol-sub", preferred_username="carol", rag_roles=["rag-curate:org-a"]
        )
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert b"Reviewing submissions" in resp.body
        assert b"Permanent destruction" not in resp.body

    def test_purge_holder_sees_only_the_purge_article(self, db):
        user = UserClaims(sub="dave-sub", preferred_username="dave", rag_roles=["rag-purge"])
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert b"Permanent destruction" in resp.body
        assert b"Reviewing submissions" not in resp.body

    def test_admin_sees_only_the_admin_article(self, db):
        user = UserClaims(sub="eve-sub", preferred_username="eve", rag_roles=["rag-admin"])
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert b"Portal configuration" in resp.body
        assert b"Permanent destruction" not in resp.body

    def test_user_with_multiple_roles_sees_the_union(self, db):
        user = UserClaims(
            sub="frank-sub",
            preferred_username="frank",
            rag_roles=["rag-ingest", "rag-curate:org-a", "rag-purge"],
        )
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert b"Submitting documents" in resp.body
        assert b"Reviewing submissions" in resp.body
        assert b"Permanent destruction" in resp.body
        assert b"Searching the corpus" not in resp.body
        assert b"Portal configuration" not in resp.body

    def test_user_with_no_capability_roles_sees_no_role_articles(self, db):
        """A signed-in user who holds none of the five capability roles yet
        (e.g. mid-provisioning) still gets a 200, with a message pointing at
        who to ask, rather than an empty or broken page."""
        user = UserClaims(sub="grace-sub", preferred_username="grace", rag_roles=[])
        resp = main.kb_page(_request(), session=db, current_user=user)

        assert resp.status_code == 200
        assert b"doesn't hold any of this portal's capability roles yet" in resp.body
        assert b"Submitting documents" not in resp.body
        assert b"Searching the corpus" not in resp.body
        assert b"Reviewing submissions" not in resp.body
        assert b"Permanent destruction" not in resp.body
        assert b"Portal configuration" not in resp.body
