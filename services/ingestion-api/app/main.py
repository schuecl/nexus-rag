from __future__ import annotations

from contextlib import asynccontextmanager

from app.routes import admin, curate, upload
from common.db import get_engine, init_db
from common.models import ClassificationLevel, ReleasabilityValue
from fastapi import FastAPI, Request
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

app.include_router(upload.router)
app.include_router(curate.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def upload_page(request: Request):
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "classifications": DEFAULT_CLASSIFICATIONS,
            "releasability": DEFAULT_RELEASABILITY,
        },
    )


@app.get("/curate", response_class=HTMLResponse)
def curate_page(request: Request):
    return templates.TemplateResponse(request, "curate.html", {})
