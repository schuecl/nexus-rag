"""C9: admin-configurable Classification/Releasability lists -- add, retire, or
reorder without a code change or redeploy."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.deps import require_admin, verify_csrf
from common.claims import UserClaims
from common.db import get_session
from common.models import ClassificationLevel, ReleasabilityValue

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
    row = session.exec(
        select(ReleasabilityValue).where(ReleasabilityValue.value == value)
    ).first()
    if row:
        row.active = False
        session.add(row)
        session.commit()
    return {"retired": value}
