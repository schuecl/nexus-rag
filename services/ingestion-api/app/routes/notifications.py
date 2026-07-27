"""FR-15: in-app notifications for curator decisions on the uploader's own
submissions. Written by app/routes/curate.py at approve/reject time; this
module is just the read side (list + mark-read), scoped to the recipient."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.deps import get_current_user, verify_csrf
from common.claims import UserClaims
from common.db import get_session
from common.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def list_notifications(
    unread_only: bool = False,
    user: UserClaims = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Sequence[Notification]:
    query = select(Notification).where(Notification.recipient_sub == user.sub)
    if unread_only:
        query = query.where(Notification.read == False)  # noqa: E712
    # SQLModel table classes use plain annotations rather than SQLAlchemy
    # 2.0's Mapped[], so mypy sees Notification.created_at as a bare
    # datetime -- not a real bug, see pyproject.toml's mypy section.
    query = query.order_by(Notification.created_at.desc())  # type: ignore[attr-defined]
    return session.exec(query).all()


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: uuid.UUID,
    user: UserClaims = Depends(get_current_user),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Notification:
    notification = session.get(Notification, notification_id)
    if notification is None or notification.recipient_sub != user.sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    notification.read = True
    session.add(notification)
    session.commit()
    session.refresh(notification)
    return notification
