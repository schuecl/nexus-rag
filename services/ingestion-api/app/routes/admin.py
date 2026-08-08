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
    """The single deployment-wide settings row, created on first read.

    Named for its original purpose (the classification banner) but now also
    backs the theme, branding, and login popup-banner settings below -- all of
    them are the same "one row, admin-set, deployment-wide" shape, so a second
    table would just be this one with different column names.

    Inactive is the correct default for the classification banner
    specifically: "no authority has set a marking" is not the same statement
    as "this system holds unclassified material", and defaulting to the
    second is how a wrong marking reaches a screen.
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
THEMES = frozenset({"", "midnight", "phosphor", "slate", "amber", "daylight", "dracula"})


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


# --------------------------------------------------------------------------
# Issue #424: per-identity upload quota (NFR-17 residual).
# --------------------------------------------------------------------------


class UploadQuotaIn(BaseModel):
    """Both caps, in the units an admin thinks in.

    Bytes are entered as GiB because the stored column is bytes and nobody sets
    a storage policy in bytes; the conversion lives here so the enforcement path
    never has to care.
    """

    max_inflight: int
    max_bytes_24h_gib: float


@router.post("/upload-quota")
def set_upload_quota(
    body: UploadQuotaIn,
    user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> PortalSettings:
    """Issue #424: the per-identity upload caps.

    0 means unlimited for either cap -- an explicit choice for a deployment that
    bounds this at the platform layer instead, and deliberately not the default
    (see app/quota.py: a null column resolves to the module default, so upgrading
    into this release gains the bound rather than keeping the gap).

    Negative values are rejected rather than clamped: a negative cap is a typo,
    and silently reading it as 0 would turn a fat-fingered "-1" into "unlimited",
    which is the opposite of what the person meant.
    """
    if body.max_inflight < 0 or body.max_bytes_24h_gib < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "quota values cannot be negative; use 0 for unlimited",
        )
    max_bytes = int(body.max_bytes_24h_gib * 1024 * 1024 * 1024)
    settings = _load_banner(session)
    settings.upload_quota_max_inflight = body.max_inflight
    settings.upload_quota_max_bytes_24h = max_bytes
    settings.updated_by = user.preferred_username
    settings.updated_at = datetime.now(UTC)
    session.add(settings)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="admin.upload_quota_set",
            target_id="portal_settings",
            detail={"max_inflight": body.max_inflight, "max_bytes_24h": max_bytes},
        )
    )
    session.commit()
    session.refresh(settings)
    return settings


# --------------------------------------------------------------------------
# Issue #248: branding (application name + logo) and the login landing
# page's (#246) mandatory-acceptance warning popup.
# --------------------------------------------------------------------------


class BrandingIn(BaseModel):
    app_name: str = ""
    logo_url: str = ""


@router.post("/branding")
def set_branding(
    body: BrandingIn,
    user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> PortalSettings:
    settings = _load_banner(session)
    settings.app_name = body.app_name.strip()
    settings.logo_url = body.logo_url.strip()
    settings.updated_by = user.preferred_username
    settings.updated_at = datetime.now(UTC)
    session.add(settings)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="admin.branding_set",
            target_id="portal_settings",
            detail={"app_name": settings.app_name, "logo_url": settings.logo_url},
        )
    )
    session.commit()
    session.refresh(settings)
    return settings


class LoginBannerIn(BaseModel):
    title: str = ""
    text: str
    active: bool = True
    button_text: str = ""


@router.post("/login-banner")
def set_login_banner(
    body: LoginBannerIn,
    user: UserClaims = Depends(require_admin),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> PortalSettings:
    """The login landing page's (#246) mandatory-acceptance popup -- distinct
    from the CAPCO classification banner above. An empty text cannot be
    active, same reasoning as set_banner: a popup with nothing in it is a
    broken dialog, not the absence of one. The title has no such rule -- it's
    cosmetic (login.html falls back to "Notice" when unset), not what gates
    whether the popup shows at all.
    """
    settings = _load_banner(session)
    settings.login_popup_title = body.title.strip()
    settings.login_popup_text = body.text.strip()
    settings.login_popup_active = body.active and bool(settings.login_popup_text)
    settings.login_button_text = body.button_text.strip()
    settings.updated_by = user.preferred_username
    settings.updated_at = datetime.now(UTC)
    session.add(settings)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="admin.login_banner_set",
            target_id="portal_settings",
            detail={
                "login_popup_title": settings.login_popup_title,
                "login_popup_text": settings.login_popup_text,
                "login_popup_active": settings.login_popup_active,
                "login_button_text": settings.login_button_text,
            },
        )
    )
    session.commit()
    session.refresh(settings)
    return settings
