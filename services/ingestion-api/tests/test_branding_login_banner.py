"""Issue #248: admin-configurable branding (app name, logo) and the login
landing page's (#246) mandatory-acceptance warning popup.

Admin route functions are called directly with explicit arguments, the same
way test_session_lifecycle.py exercises deps/auth internals and
test_login_gate.py exercises the page routes -- Depends() is just a default
parameter value, so this reaches the real persistence/audit-log logic
without needing TestClient or a live app (main.py's lifespan opens a real
NATS connection this suite has no business standing up).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.requests import Request

from app import main
from app.routes import admin
from app.routes.admin import BrandingIn, LoginBannerIn
from common.claims import UserClaims
from common.models import AuditLogEntry, PortalSettings


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()  # #188: undisposed sqlite3 connections fail an unrelated test at GC time


@pytest.fixture
def admin_user() -> UserClaims:
    return UserClaims(sub="dave-sub", preferred_username="dave-admin", rag_roles=["rag-admin"])


def _request(path: str = "/") -> Request:
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


class TestSetBranding:
    def test_persists_app_name_and_logo_url(self, db, admin_user):
        result = admin.set_branding(
            BrandingIn(app_name="Nexus Portal", logo_url="https://example.mil/logo.png"),
            user=admin_user,
            session=db,
            _csrf=None,
        )

        assert result.app_name == "Nexus Portal"
        assert result.logo_url == "https://example.mil/logo.png"

    def test_strips_whitespace(self, db, admin_user):
        result = admin.set_branding(
            BrandingIn(app_name="  Nexus Portal  ", logo_url="  https://example.mil/logo.png  "),
            user=admin_user,
            session=db,
            _csrf=None,
        )

        assert result.app_name == "Nexus Portal"
        assert result.logo_url == "https://example.mil/logo.png"

    def test_writes_an_audit_log_entry(self, db, admin_user):
        admin.set_branding(
            BrandingIn(app_name="Nexus Portal", logo_url=""),
            user=admin_user,
            session=db,
            _csrf=None,
        )

        entries = db.exec(select(AuditLogEntry)).all()
        assert [e.action for e in entries] == ["admin.branding_set"]
        assert entries[0].detail["app_name"] == "Nexus Portal"


class TestSetLoginBanner:
    def test_persists_title_text_button_and_active_flag(self, db, admin_user):
        result = admin.set_login_banner(
            LoginBannerIn(
                title="Consent to monitoring",
                text="Authorized use only.",
                active=True,
                button_text="Enter",
            ),
            user=admin_user,
            session=db,
            _csrf=None,
        )

        assert result.login_popup_title == "Consent to monitoring"
        assert result.login_popup_text == "Authorized use only."
        assert result.login_popup_active is True
        assert result.login_button_text == "Enter"

    def test_empty_text_cannot_be_active(self, db, admin_user):
        """Mirrors set_banner's rule: a popup with nothing in it is a broken
        dialog, not the absence of one."""
        result = admin.set_login_banner(
            LoginBannerIn(text="  ", active=True, button_text=""),
            user=admin_user,
            session=db,
            _csrf=None,
        )

        assert result.login_popup_text == ""
        assert result.login_popup_active is False

    def test_writes_an_audit_log_entry(self, db, admin_user):
        admin.set_login_banner(
            LoginBannerIn(text="Notice", active=True, button_text=""),
            user=admin_user,
            session=db,
            _csrf=None,
        )

        entries = db.exec(select(AuditLogEntry)).all()
        assert [e.action for e in entries] == ["admin.login_banner_set"]
        assert entries[0].detail["login_popup_active"] is True


class TestLoginPageRendersBranding:
    def _seed(self, db, **overrides):
        settings = PortalSettings(
            id=1,
            app_name="Nexus Portal",
            logo_url="https://example.mil/logo.png",
            login_button_text="Enter",
            **overrides,
        )
        db.add(settings)
        db.commit()

    def test_app_name_and_logo_appear_on_the_login_page(self, db):
        self._seed(db)

        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b"Nexus Portal" in resp.body
        assert b"https://example.mil/logo.png" in resp.body
        assert b"Enter" in resp.body

    def test_branding_appears_in_the_header_on_a_real_page_too(self, db):
        """Issue #248: app_name/logo_url are deployment-wide, not just for
        the login page -- an authenticated visitor's header/tab title/
        favicon should reflect them too."""
        self._seed(db)
        user = UserClaims(sub="alice-sub", preferred_username="alice")

        resp = main.upload_page(_request(), session=db, current_user=user)

        assert b"Nexus Portal" in resp.body
        assert b'rel="icon" href="https://example.mil/logo.png"' in resp.body

    def test_default_button_text_is_used_when_unset(self, db):
        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b"Login via OIDC" in resp.body

    def test_inactive_popup_leaves_the_login_button_visible(self, db):
        self._seed(db, login_popup_text="Unused while inactive", login_popup_active=False)

        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b'id="loginButton"\n       hidden' not in resp.body
        assert b"bannerModalBackdrop" not in resp.body

    def test_active_popup_hides_the_login_button_until_accepted(self, db):
        self._seed(db, login_popup_text="Authorized use only.", login_popup_active=True)

        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b"Authorized use only." in resp.body
        assert b"bannerModalBackdrop" in resp.body
        assert b'id="loginButton"\n       hidden' in resp.body

    def test_popup_title_defaults_to_notice_when_unset(self, db):
        self._seed(db, login_popup_text="Authorized use only.", login_popup_active=True)

        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b'id="bannerModalTitle">Notice<' in resp.body

    def test_popup_title_is_configurable(self, db):
        self._seed(
            db,
            login_popup_title="Consent to monitoring",
            login_popup_text="Authorized use only.",
            login_popup_active=True,
        )

        resp = main.upload_page(_request(), session=db, current_user=None)

        assert b'id="bannerModalTitle">Consent to monitoring<' in resp.body


class TestLoginDeclinedPage:
    def test_renders_without_requiring_a_session(self, db):
        resp = main.login_declined_page(_request("/login/declined"), session=db)

        assert resp.status_code == 200
        assert b"mandatory" in resp.body.lower()
