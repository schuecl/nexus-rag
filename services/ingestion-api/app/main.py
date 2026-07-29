from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import metrics
from app.deps import get_current_user_optional
from app.recovery import reconcile_forever
from app.routes import admin, auth, curate, notifications, search, upload
from common.claims import UserClaims
from common.db import get_engine, get_session, init_db
from common.job_queue import ensure_stream, get_nats_connection
from common.logging_setup import setup_logging
from common.metadata import NO_RELEASABILITY_RESTRICTION
from common.models import ClassificationLevel, PortalSettings, ReleasabilityValue
from common.siem import enable_siem_export
from common.tracing import setup_tracing

# #73: level-configurable structured logging (LOG_LEVEL/LOG_FORMAT), and NFR-2
# SIEM export of every audit event this service writes (upload, curation,
# purge, auth). Module level, before the app object exists, so startup logging
# is already formatted and filtered.
setup_logging("ingestion-api")
enable_siem_export("ingestion-api")
# #134: request spans (FastAPI auto-instrumentation, applied to the app after
# it is created below) plus the manual ingest.submit span in routes/upload.py
# whose context rides the NATS headers to ingestion-worker. Disabled unless
# OTEL_EXPORTER_OTLP_ENDPOINT is set.
setup_tracing("ingestion-api")

DEFAULT_CLASSIFICATIONS = [
    ("UNCLASSIFIED", 0),
    ("CUI", 1),
    ("SECRET", 2),
]
# NO_RELEASABILITY_RESTRICTION (common/metadata.py) must stay first in this
# controlled vocabulary so it's the default-selected <option> on the upload
# form -- the un-set state, not a coalition caveat, is what most documents
# should carry.
DEFAULT_RELEASABILITY = [NO_RELEASABILITY_RESTRICTION, "NOFORN", "USA", "NATO", "FVEY"]


def _seed_defaults() -> None:
    """Dev convenience only: seed the admin-configurable lists (C9) with the
    example values from REQUIREMENTS.md Section 6.3 if the tables are empty."""
    with Session(get_engine()) as session:
        if not session.exec(select(ClassificationLevel)).first():
            for value, rank in DEFAULT_CLASSIFICATIONS:
                session.add(ClassificationLevel(value=value, rank=rank))
        if not session.exec(select(ReleasabilityValue)).first():
            for value in DEFAULT_RELEASABILITY:
                session.add(ReleasabilityValue(value=value))
        session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    _seed_defaults()
    # NFR-11: one long-lived JetStream connection for the process, not a
    # reconnect per request -- app/routes/upload.py publishes through this
    # via request.app.state.jetstream.
    nc = await get_nats_connection()
    js = nc.jetstream()
    await ensure_stream(js)
    app.state.jetstream = js
    recovery_task = asyncio.create_task(reconcile_forever(js))
    try:
        yield
    finally:
        recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await recovery_task
        await nc.close()


APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="nexus-rag ingestion-api", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.middleware("http")(metrics.http_metrics_middleware)
# #134: one request span per route, with incoming traceparent honored; the
# import lives here rather than at the top so the app object exists first.
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: E402

FastAPIInstrumentor.instrument_app(app)
templates = Jinja2Templates(directory=APP_DIR / "templates")

app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(curate.router)
app.include_router(admin.router)
app.include_router(notifications.router)
app.include_router(search.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    payload, content_type = metrics.render()
    return Response(payload, media_type=content_type)


def _live_controlled_vocab(session: Session) -> dict:
    # C9: the *live*, admin-configurable lists (retired values excluded) --
    # not the DEFAULT_* constants, which only seed those tables on first boot
    # (see _seed_defaults above). Shared by the upload page and the curation
    # queue page, since FR-13's "correct" action assigns the same
    # Classification/Releasability values FR-17 requires come from a
    # controlled vocabulary, not free text -- same as at upload time.
    # SQLModel table classes use plain annotations rather than SQLAlchemy
    # 2.0's Mapped[], so mypy sees ClassificationLevel.rank as a bare int --
    # not a real bug, see pyproject.toml's mypy section.
    classifications = session.exec(
        select(ClassificationLevel)
        .where(ClassificationLevel.active == True)  # noqa: E712
        .order_by(ClassificationLevel.rank)  # type: ignore[arg-type]
    ).all()
    releasability = session.exec(
        select(ReleasabilityValue).where(ReleasabilityValue.active == True)  # noqa: E712
    ).all()
    return {
        "classifications": [(c.value, c.rank) for c in classifications],
        "releasability": [r.value for r in releasability],
        "no_releasability_restriction": NO_RELEASABILITY_RESTRICTION,
    }


def _asset_version() -> str:
    """Cache-busting suffix for the stylesheet, from its own content.

    Issue #166: a theme change looked like it had not applied because the
    browser was still serving the previous portal.css -- the link carried no
    version, so nothing told the cache the file had changed. That cost a real
    debugging round chasing a CSS bug that was not there.

    Hashing the file rather than using a timestamp means the URL only changes
    when the bytes do, so an unchanged deploy keeps the cache warm.
    """
    css = Path(__file__).parent / "static" / "portal.css"
    try:
        return hashlib.sha256(css.read_bytes()).hexdigest()[:12]
    except OSError:  # pragma: no cover - the file ships with the image
        return "dev"


ASSET_VERSION = _asset_version()


def _page_context(session: Session, current_user: UserClaims | None) -> dict:
    """Context every rendered page needs.

    Issue #166: the classification banner goes through here rather than each
    route adding it, because a banner missing from one page is a real defect,
    not a cosmetic one -- and four routes each assembling their own dict is
    exactly how one of them ends up without it.

    An unset or inactive banner is passed through as None. base.html then says
    so explicitly instead of falling back to a level: "no marking has been
    configured" and "this system holds unclassified material" are different
    statements, and rendering the second when the first is true puts a wrong
    marking on a screen.
    """
    settings = session.get(PortalSettings, 1)
    return {
        "asset_version": ASSET_VERSION,
        "current_user": current_user,
        "banner": settings if (settings and settings.active and settings.text) else None,
        # Empty string is the built-in default; base.html emits it as a
        # data-theme attribute and portal.css keys its token block off it.
        "theme": (settings.theme if settings else "") or "",
        # Issue #248: branding shown persistently (header, tab title,
        # favicon), not just on the login page -- so this lives in the
        # context every page gets, not just _login_page's below.
        "app_name": (settings.app_name if settings else "") or DEFAULT_APP_NAME,
        "logo_url": (settings.logo_url if settings else "") or None,
    }


# Issue #246/#248: defaults for the login page's branding/button text, used
# whenever no admin has set a PortalSettings value yet.
DEFAULT_APP_NAME = "Document Portal"
DEFAULT_LOGIN_BUTTON_TEXT = "Login via OIDC"
DEFAULT_LOGIN_POPUP_TITLE = "Notice"


def _login_page(request: Request, session: Session) -> HTMLResponse:
    """Issue #246: every page route renders this instead of its real content
    for an anonymous visitor -- the app no longer shows a fully-featured page
    that then fails on the first action a logged-out visitor takes.

    Takes session (not a pre-built context) because it still needs the
    classification banner: that marking is a property of the deployment, not
    of whoever is or isn't signed in, so it belongs on this page too.
    """
    settings = session.get(PortalSettings, 1)
    ctx = _page_context(session, None)
    ctx["login_button_text"] = (settings.login_button_text if settings else "") or (
        DEFAULT_LOGIN_BUTTON_TEXT
    )
    # Issue #248: the mandatory-acceptance popup. Mirrors _page_context's
    # classification-banner treatment -- inactive-or-unset is passed through
    # as a clean "don't render anything", not a default message.
    ctx["login_popup_text"] = (
        settings.login_popup_text if (settings and settings.login_popup_active) else ""
    )
    ctx["login_popup_title"] = (settings.login_popup_title if settings else "") or (
        DEFAULT_LOGIN_POPUP_TITLE
    )
    return templates.TemplateResponse(request, "login.html", ctx)


@app.get("/login/declined", response_class=HTMLResponse)
def login_declined_page(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Issue #248: where a visitor lands after declining the mandatory
    acceptance popup. No current_user dependency at all -- a visitor who just
    declined the banner is by definition not signed in yet, and this page
    itself requires no authority to view."""
    return templates.TemplateResponse(request, "login_declined.html", _page_context(session, None))


@app.get("/", response_class=HTMLResponse)
def upload_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    ctx = _live_controlled_vocab(session)
    ctx.update(_page_context(session, current_user))
    return templates.TemplateResponse(request, "upload.html", ctx)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    """Issue #166: the UI for the settings /admin/* already exposed as an API.

    Signed-in but non-admin visitors still see this page render (issue #246
    only gates on *authentication*, not on the rag-admin role) -- every action
    on it goes through /admin/*, which is behind require_admin, and the page
    shows the resulting 403 rather than pretending the route does not exist.
    Role authorization belongs on the endpoints that change state, not on the
    HTML that describes them -- and a 404 for a non-admin would be a worse lie
    than an honest "you do not hold this role".
    """
    if current_user is None:
        return _login_page(request, session)
    return templates.TemplateResponse(request, "admin.html", _page_context(session, current_user))


@app.get("/curate", response_class=HTMLResponse)
def curate_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    ctx = _live_controlled_vocab(session)
    ctx.update(_page_context(session, current_user))
    return templates.TemplateResponse(request, "curate.html", ctx)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    return templates.TemplateResponse(
        request, "notifications.html", _page_context(session, current_user)
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    return templates.TemplateResponse(request, "search.html", _page_context(session, current_user))
