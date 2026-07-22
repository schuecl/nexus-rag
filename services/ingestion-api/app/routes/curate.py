"""FR-10..FR-16: curation queue, scoped to the orgs a curator holds authority
for (FR-12), with approval capped by both org (FR-14.2) and clearance (FR-14.1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.deps import allowed_classifications, require_curator
from common.db import get_session
from common.models import AuditLogEntry, Document
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

router = APIRouter(prefix="/curate", tags=["curation"])


@router.get("/queue")
def list_queue(
    user=Depends(require_curator),
    session: Session = Depends(get_session),
):
    docs = session.exec(
        select(Document)
        .where(Document.status == "pending_review")
        .where(Document.owner_org.in_(user.curatable_orgs))  # type: ignore[attr-defined]
    ).all()
    return docs


def _load_pending(session: Session, doc_id: uuid.UUID) -> Document:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc.status != "pending_review":
        raise HTTPException(status.HTTP_409_CONFLICT, f"document is already {doc.status}")
    return doc


def _check_curator_authority(user, doc: Document, session: Session) -> None:
    if not user.can_curate_org(doc.owner_org):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"not a curator for org '{doc.owner_org}'"
        )
    allowed = allowed_classifications(session, user.clearance)
    if doc.classification not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cannot approve a document above your own cleared level",
        )


class Corrections(BaseModel):
    classification: str | None = None
    releasability: list[str] | None = None
    access_scope: list[str] | None = None


@router.post("/{doc_id}/approve")
def approve(
    doc_id: uuid.UUID,
    corrections: Corrections | None = None,
    user=Depends(require_curator),
    session: Session = Depends(get_session),
):
    doc = _load_pending(session, doc_id)
    _check_curator_authority(user, doc, session)

    if corrections:
        if corrections.classification:
            doc.classification = corrections.classification
        if corrections.releasability is not None:
            doc.releasability = corrections.releasability
        if corrections.access_scope is not None:
            doc.access_scope = corrections.access_scope
        # Re-check authority against the corrected classification, not just the original.
        _check_curator_authority(user, doc, session)

    doc.status = "approved"
    doc.reviewed_by_sub = user.sub
    doc.reviewed_at = datetime.now(timezone.utc)
    session.add(doc)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.approve",
            target_id=str(doc.id),
            detail={"corrections": corrections.model_dump() if corrections else None},
        )
    )
    session.commit()
    session.refresh(doc)
    return doc


class Rejection(BaseModel):
    reason: str


@router.post("/{doc_id}/reject")
def reject(
    doc_id: uuid.UUID,
    body: Rejection,
    user=Depends(require_curator),
    session: Session = Depends(get_session),
):
    doc = _load_pending(session, doc_id)
    _check_curator_authority(user, doc, session)

    doc.status = "rejected"
    doc.rejection_reason = body.reason
    doc.reviewed_by_sub = user.sub
    doc.reviewed_at = datetime.now(timezone.utc)
    session.add(doc)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.reject",
            target_id=str(doc.id),
            detail={"reason": body.reason},
        )
    )
    session.commit()
    session.refresh(doc)
    return doc
