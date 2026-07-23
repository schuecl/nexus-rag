from __future__ import annotations

from contextlib import asynccontextmanager

from app.routes import admin, auth, curate, notifications, upload
from common.db import get_engine, get_session, init_db
from common.models import ClassificationLevel, ReleasabilityValue
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

DEFAULT_CLASSIFICATIONS = [
    ("UNCLASSIFIED", 0),
    ("CUI", 1),
    ("CONFIDENTIAL", 2),
    ("SECRET", 3),
    ("TOP SECRET", 4),
]
DEFAULT_RELEASABILITY = ["NOFORN", "REL TO USA/FVEY", "REL TO NATO"]


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
async def lifespan(_app: FastAPI):
    init_db()
    _seed_defaults()
    yield


app = FastAPI(title="nexus-rag ingestion-api", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(curate.router)
app.include_router(admin.router)
app.include_router(notifications.router)


@app.get("/health")
def health():
    return {"status": "ok"}


def _live_controlled_vocab(session: Session) -> dict:
    # C9: the *live*, admin-configurable lists (retired values excluded) --
    # not the DEFAULT_* constants, which only seed those tables on first boot
    # (see _seed_defaults above). Shared by the upload page and the curation
    # queue page, since FR-13's "correct" action assigns the same
    # Classification/Releasability values FR-17 requires come from a
    # controlled vocabulary, not free text -- same as at upload time.
    classifications = session.exec(
        select(ClassificationLevel)
        .where(ClassificationLevel.active == True)  # noqa: E712
        .order_by(ClassificationLevel.rank)
    ).all()
    releasability = session.exec(
        select(ReleasabilityValue).where(ReleasabilityValue.active == True)  # noqa: E712
    ).all()
    return {
        "classifications": [(c.value, c.rank) for c in classifications],
        "releasability": [r.value for r in releasability],
    }


@app.get("/", response_class=HTMLResponse)
def upload_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "upload.html", _live_controlled_vocab(session))


@app.get("/curate", response_class=HTMLResponse)
def curate_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "curate.html", _live_controlled_vocab(session))


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    return templates.TemplateResponse(request, "notifications.html", {})
