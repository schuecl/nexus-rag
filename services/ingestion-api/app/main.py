from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import metrics
from app.csp import ContentSecurityPolicyMiddleware
from app.deps import get_current_user_optional
from app.recovery import reconcile_forever
from app.routes import admin, auth, curate, notifications, search, upload
from common.claims import UserClaims
from common.db import get_engine, get_session, init_db
from common.job_queue import ensure_stream, get_nats_connection
from common.logging_setup import setup_logging
from common.metadata import NO_RELEASABILITY_RESTRICTION
from common.models import ClassificationLevel, PortalSettings, ReleasabilityValue
from common.profiling import setup_profiling
from common.security_headers import SecurityHeadersMiddleware
from common.siem import enable_siem_export
from common.tracing import setup_tracing

# #73: level-configurable structured logging (LOG_LEVEL/LOG_FORMAT), and NFR-2
# SIEM export of every audit event this service writes (upload, curation,
# purge, auth). Module level, before the app object exists, so startup logging
# is already formatted and filtered.
setup_logging("ingestion-api")
logger = logging.getLogger("ingestion-api")
enable_siem_export("ingestion-api")
# #134: request spans (FastAPI auto-instrumentation, applied to the app after
# it is created below) plus the manual ingest.submit span in routes/upload.py
# whose context rides the NATS headers to ingestion-worker. Disabled unless
# OTEL_EXPORTER_OTLP_ENDPOINT is set.
setup_tracing("ingestion-api")
# #349: continuous CPU profiling. Disabled unless PYROSCOPE_SERVER_ADDRESS
# is set.
setup_profiling("ingestion-api")

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


class VocabularyConfigError(RuntimeError):
    """A deploy-time vocabulary override that could not be parsed.

    Raised at startup rather than warned about and skipped. Issue #564: the
    failure this guards is a site whose real marking scheme differs booting into
    the dev example vocabulary -- uploads then get tagged and filtered against
    values that do not exist in that environment's Keycloak
    ``rag-clearance:*``/``rag-releasability:*`` roles, and the mismatch surfaces
    as silently empty retrieval results rather than an error. Falling back to
    the dev defaults on a typo would reproduce exactly that, so a malformed
    override has to stop the service instead.
    """


def _parse_classification_override(raw: str) -> list[tuple[str, int]]:
    """``"UNCLASSIFIED:0,CUI:1,SECRET:2"`` -> ``[("UNCLASSIFIED", 0), ...]``.

    Rank is explicit rather than positional: it is the ordering the clearance
    ceiling is evaluated against (FR-26), so leaving it implicit in list order
    would make a reordered env var silently change who can read what.
    """
    entries: list[tuple[str, int]] = []
    seen: set[str] = set()
    ranks: set[int] = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        value, sep, rank_text = item.partition(":")
        value = value.strip()
        if not sep or not value:
            raise VocabularyConfigError(
                f"CLASSIFICATION_LEVELS entry {item!r} is not 'VALUE:rank' -- "
                "e.g. CLASSIFICATION_LEVELS='UNCLASSIFIED:0,CUI:1,SECRET:2'"
            )
        try:
            rank = int(rank_text.strip())
        except ValueError as exc:
            raise VocabularyConfigError(
                f"CLASSIFICATION_LEVELS entry {item!r} has a non-integer rank"
            ) from exc
        if value in seen:
            raise VocabularyConfigError(f"CLASSIFICATION_LEVELS lists {value!r} more than once")
        # A duplicate rank makes the clearance ceiling ambiguous: two levels
        # would compare equal, so "at or below my clearance" stops being a
        # total order and which one a user can read depends on row order.
        if rank in ranks:
            raise VocabularyConfigError(
                f"CLASSIFICATION_LEVELS reuses rank {rank} -- ranks order the "
                "clearance ceiling (FR-26) and must be unique"
            )
        seen.add(value)
        ranks.add(rank)
        entries.append((value, rank))
    if not entries:
        raise VocabularyConfigError("CLASSIFICATION_LEVELS is set but empty")
    return entries


def _parse_releasability_override(raw: str) -> list[str]:
    """``"NONE,NATO,FVEY"`` -> ``["NONE", "NATO", "FVEY"]``."""
    values: list[str] = []
    for value in (part.strip() for part in raw.split(",")):
        if not value:
            continue
        if value in values:
            raise VocabularyConfigError(f"RELEASABILITY_VALUES lists {value!r} more than once")
        values.append(value)
    if not values:
        raise VocabularyConfigError("RELEASABILITY_VALUES is set but empty")
    if values[0] != NO_RELEASABILITY_RESTRICTION:
        # Same constraint the hardcoded default documents above: the un-set
        # state has to be the first <option> on the upload form, or the form
        # defaults every new document to a coalition caveat nobody chose.
        raise VocabularyConfigError(
            f"RELEASABILITY_VALUES must start with {NO_RELEASABILITY_RESTRICTION!r} "
            "so the un-set state is the default-selected option on the upload form"
        )
    return values


def _configured_vocabulary() -> tuple[list[tuple[str, int]], list[str]] | None:
    """The deploy-time vocabulary, or None when startup must not seed at all.

    Issue #564. Unset env vars keep the dev defaults, so the compose stack is
    unchanged; SEED_DEFAULT_VOCAB=false turns seeding off entirely for sites
    that provision the tables through the admin API or a migration and do not
    want a service writing vocabulary on boot.
    """
    if os.environ.get("SEED_DEFAULT_VOCAB", "true").strip().lower() == "false":
        return None
    raw_levels = os.environ.get("CLASSIFICATION_LEVELS", "").strip()
    raw_releasability = os.environ.get("RELEASABILITY_VALUES", "").strip()
    classifications = (
        _parse_classification_override(raw_levels) if raw_levels else DEFAULT_CLASSIFICATIONS
    )
    releasability = (
        _parse_releasability_override(raw_releasability)
        if raw_releasability
        else DEFAULT_RELEASABILITY
    )
    return classifications, releasability


def _seed_defaults() -> None:
    """Seed the admin-configurable lists (C9) if the tables are empty.

    Empty-table-only, so it stays idempotent and never fights an admin's later
    edits through routes/admin.py. What changed in #564 is where the values come
    from: CLASSIFICATION_LEVELS/RELEASABILITY_VALUES let a deployment declare
    its own vocabulary in config that can be reviewed, instead of every
    environment silently inheriting REQUIREMENTS.md Section 6.3's dev examples
    the first time ingestion-api boots against a fresh database.
    """
    configured = _configured_vocabulary()
    if configured is None:
        logger.info(
            "SEED_DEFAULT_VOCAB=false -- not seeding classification/releasability "
            "vocabulary; provision it via the admin API before first upload"
        )
        return
    classifications, releasability = configured
    with Session(get_engine()) as session:
        if not session.exec(select(ClassificationLevel)).first():
            for value, rank in classifications:
                session.add(ClassificationLevel(value=value, rank=rank))
            logger.info(
                "seeded %d classification level(s): %s",
                len(classifications),
                ", ".join(f"{v}:{r}" for v, r in classifications),
            )
        if not session.exec(select(ReleasabilityValue)).first():
            for value in releasability:
                session.add(ReleasabilityValue(value=value))
            logger.info(
                "seeded %d releasability value(s): %s",
                len(releasability),
                ", ".join(releasability),
            )
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
# #443: per-response nonce + Content-Security-Policy header on every response.
app.add_middleware(ContentSecurityPolicyMiddleware)
# #444: X-Frame-Options is this app's own -- the curation UI's approve/
# reject/correct controls are the single-click, high-consequence actions a
# framing attack targets, and NFR-14's CSRF cookie doesn't see that attack
# (a framed page is same-origin, so the cookie/header echo still matches).
# Nothing in this repo frames the portal today, so DENY over SAMEORIGIN;
# revisit if MPNexus ever needs to embed it.
app.add_middleware(
    SecurityHeadersMiddleware,
    extra_headers=((b"x-frame-options", b"DENY"),),
)
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
        # Issue #356: surfaced on the upload page so the batch-file cap shown
        # to the uploader can't drift from what POST /documents/batch enforces.
        "max_batch_files": upload.MAX_BATCH_FILES,
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


def _page_context(request: Request, session: Session, current_user: UserClaims | None) -> dict:
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
        # #443: every inline <script> block on the page must carry this same
        # per-request value (app/csp.py's ContentSecurityPolicyMiddleware) or
        # the CSP header sent alongside it blocks it from running.
        "csp_nonce": request.state.csp_nonce,
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
    ctx = _page_context(request, session, None)
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
    return templates.TemplateResponse(
        request, "login_declined.html", _page_context(request, session, None)
    )


@app.get("/", response_class=HTMLResponse)
def upload_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    ctx = _live_controlled_vocab(session)
    ctx.update(_page_context(request, session, current_user))
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
    return templates.TemplateResponse(
        request, "admin.html", _page_context(request, session, current_user)
    )


@app.get("/curate", response_class=HTMLResponse)
def curate_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    ctx = _live_controlled_vocab(session)
    ctx.update(_page_context(request, session, current_user))
    return templates.TemplateResponse(request, "curate.html", ctx)


@app.get("/curate/list", response_class=HTMLResponse)
def curate_list_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    """Issue #266: the curation "master list" -- every document a curator
    holds authority over, any status, with filtering/search and a metadata
    edit dialog (GET/PATCH /curate/documents in app/routes/curate.py). A
    separate page from /curate (the pending_review queue) rather than a tab on
    it, per the issue's own suggestion.

    Same pattern as curate_page above: role authorization (require_curator)
    lives on the API the page calls, not here -- a signed-in visitor without
    any rag-curate:<org> role still sees the page render and gets an honest
    403 from the endpoints, rather than this page pretending not to exist.
    """
    if current_user is None:
        return _login_page(request, session)
    ctx = _live_controlled_vocab(session)
    ctx.update(_page_context(request, session, current_user))
    return templates.TemplateResponse(request, "curate_list.html", ctx)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    return templates.TemplateResponse(
        request, "notifications.html", _page_context(request, session, current_user)
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    if current_user is None:
        return _login_page(request, session)
    return templates.TemplateResponse(
        request, "search.html", _page_context(request, session, current_user)
    )


@app.get("/kb", response_class=HTMLResponse)
def kb_page(
    request: Request,
    session: Session = Depends(get_session),
    current_user: UserClaims | None = Depends(get_current_user_optional),
) -> HTMLResponse:
    """FR-33: the in-app knowledge base. Same pattern as every other page
    route -- gated on authentication only, not role (main.py's established
    convention, see admin_page/curate_list_page above). Which *articles*
    render is decided inside kb.html itself, one `{% if current_user.can_* %}`
    per role, exactly like base.html already gates nav tabs -- there's no
    separate authorization endpoint to guard here, since an article is static
    prose, not a document or an action."""
    if current_user is None:
        return _login_page(request, session)
    # Issue #356: the ingest-role article references the batch file-count cap,
    # same reasoning as upload_page below -- can't drift from what
    # POST /documents/batch enforces.
    ctx = _live_controlled_vocab(session)
    ctx.update(_page_context(request, session, current_user))
    return templates.TemplateResponse(request, "kb.html", ctx)
