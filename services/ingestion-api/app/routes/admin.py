"""C9: admin-configurable Classification/Releasability lists -- add, retire, or
reorder without a code change or redeploy."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.deps import require_admin, verify_csrf
from common.claims import UserClaims
from common.db import get_session
from common.models import (
    AuditLogEntry,
    ClassificationLevel,
    PortalSettings,
    ReleasabilityValue,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/classifications")
def list_classifications(
    _user: UserClaims = Depends(require_admin), session: Session = Depends(get_session)
) -> Sequence[ClassificationLevel]:
    # SQLModel table classes use plain annotations rather than SQLAlchemy
    # 2.0's Mapped[], so mypy sees ClassificationLevel.rank as a bare int --
    # not a real bug, see pyproject.toml's mypy section.
    return session.exec(
        select(ClassificationLevel).order_by(ClassificationLevel.rank)  # type: ignore[arg-type]
    ).all()


class ClassificationIn(BaseModel):
    value: str
    rank: int


@router.post("/classifications")
def upsert_classification(
    body: ClassificationIn,
    _user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> ClassificationLevel:
    existing = session.exec(
        select(ClassificationLevel).where(ClassificationLevel.value == body.value)
    ).first()
    if existing:
        existing.rank = body.rank
        existing.active = True
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    row = ClassificationLevel(value=body.value, rank=body.rank)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/classifications/{value}")
def retire_classification(
    value: str,
    _user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, str]:
    row = session.exec(
        select(ClassificationLevel).where(ClassificationLevel.value == value)
    ).first()
    if row:
        row.active = False
        session.add(row)
        session.commit()
    return {"retired": value}


@router.get("/releasability")
def list_releasability(
    _user: UserClaims = Depends(require_admin), session: Session = Depends(get_session)
) -> Sequence[ReleasabilityValue]:
    return session.exec(select(ReleasabilityValue)).all()


class ReleasabilityIn(BaseModel):
    value: str


@router.post("/releasability")
def upsert_releasability(
    body: ReleasabilityIn,
    _user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> ReleasabilityValue:
    existing = session.exec(
        select(ReleasabilityValue).where(ReleasabilityValue.value == body.value)
    ).first()
    if existing:
        existing.active = True
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    row = ReleasabilityValue(value=body.value)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/releasability/{value}")
def retire_releasability(
    value: str,
    _user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, str]:
    row = session.exec(select(ReleasabilityValue).where(ReleasabilityValue.value == value)).first()
    if row:
        row.active = False
        session.add(row)
        session.commit()
    return {"retired": value}


# --------------------------------------------------------------------------
# Issue #166: the portal's classification banner.
#
# Deliberately admin-set rather than derived from the signed-in user's
# clearance. A marking states what the *system* is accredited to hold -- a
# deployment property an accrediting authority decides. Deriving it per-viewer
# would put different markings on the same page for different people, which is
# the one thing a marking must never do.
# --------------------------------------------------------------------------


def _load_banner(session: Session) -> PortalSettings:
    """The single banner row, created inactive on first read.

    Inactive is the correct default: "no authority has set a marking" is not
    the same statement as "this system holds unclassified material", and
    defaulting to the second is how a wrong marking reaches a screen.
    """
    banner = session.get(PortalSettings, 1)
    if banner is None:
        banner = PortalSettings(id=1, text="", level="", active=False)
        session.add(banner)
        session.commit()
        session.refresh(banner)
    return banner


@router.get("/banner")
def get_banner(
    _user: UserClaims = Depends(require_admin), session: Session = Depends(get_session)
) -> PortalSettings:
    return _load_banner(session)


# The stylesheet defines a token block per theme; anything not in this set has
# no block and would silently render as the default, so it is rejected rather
# than accepted-and-ignored.
THEMES = frozenset({"", "midnight", "phosphor", "slate", "amber", "daylight"})


class BannerIn(BaseModel):
    text: str
    level: str = ""
    active: bool = True


@router.post("/banner")
def set_banner(
    body: BannerIn,
    user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> PortalSettings:
    banner = _load_banner(session)
    banner.text = body.text.strip()
    banner.level = body.level.strip()
    # An empty marking cannot be "active" -- that would render a coloured bar
    # with nothing in it, which reads as a marking rather than the absence of
    # one. Clearing the text is how an admin removes the banner.
    banner.active = body.active and bool(banner.text)
    banner.updated_by = user.preferred_username
    banner.updated_at = datetime.now(UTC)
    session.add(banner)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="admin.banner_set",
            target_id="portal_banner",
            # The marking itself is the point of the record: an accreditation
            # question later is "what did this display, and who set it".
            detail={"text": banner.text, "level": banner.level, "active": banner.active},
        )
    )
    session.commit()
    session.refresh(banner)
    return banner


class ThemeIn(BaseModel):
    theme: str


@router.post("/theme")
def set_theme(
    body: ThemeIn,
    user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> PortalSettings:
    """Issue #166: the portal's visual theme.

    Deployment-wide rather than per-user, deliberately. Two people looking at
    the same classified record should see the same page; a per-user theme is a
    small step towards them not doing so, and the classification banner's
    colours in particular must mean the same thing to everyone.
    """
    theme = body.theme.strip().lower()
    if theme not in THEMES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown theme {theme!r}; choose one of {sorted(t for t in THEMES if t)}",
        )
    settings = _load_banner(session)
    settings.theme = theme
    settings.updated_by = user.preferred_username
    settings.updated_at = datetime.now(UTC)
    session.add(settings)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="admin.theme_set",
            target_id="portal_settings",
            detail={"theme": theme or "default"},
        )
    )
    session.commit()
    session.refresh(settings)
    return settings
