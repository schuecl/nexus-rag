"""FR-10..FR-16: curation queue, scoped to the orgs a curator holds authority
for (FR-12), with approval capped by both org (FR-14.2) and clearance (FR-14.1).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.deps import allowed_classifications, require_curator
from common.db import get_session
from common.models import AuditLogEntry, Document, Notification
from common.qdrant_store import delete_document_chunks, get_qdrant_client, update_document_payload
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


def _validate_supersede(user, new_doc: Document, session: Session) -> Document:
    """FR-7: everything that can fail about the version swap, checked *before*
    any mutation (Postgres or Qdrant) happens for either document -- so a
    rejected approval attempt never leaves the new document's chunks flipped
    to `approved` in Qdrant while Postgres still says `pending_review`.

    Re-validates the old document independently rather than trusting the
    submission-time check in app/routes/upload.py: its status could have
    changed since (someone else superseded or otherwise touched it), and a
    curator's authority over the *new* doc's (possibly corrected) tags
    doesn't imply authority over the old doc's -- a version can legitimately
    change classification, so both have to be checked.
    """
    old_doc = session.get(Document, new_doc.supersedes_document_id)
    if old_doc is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the document this submission supersedes no longer exists"
        )
    if old_doc.status != "approved":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the document this submission supersedes is now '{old_doc.status}', not "
            "'approved' -- resolve manually before approving this version",
        )
    _check_curator_authority(user, old_doc, session)
    return old_doc


def _execute_supersede(user, old_doc: Document, new_doc: Document, session: Session) -> None:
    """The actual swap -- only called after _validate_supersede has already
    passed, so this is expected not to fail."""
    delete_document_chunks(get_qdrant_client(), str(old_doc.id))
    old_doc.status = "superseded"
    old_doc.updated_at = datetime.now(timezone.utc)
    session.add(old_doc)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.supersede",
            target_id=str(old_doc.id),
            detail={"superseded_by_document_id": str(new_doc.id)},
        )
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

    # FR-7: validate the whole supersede chain *before* touching Qdrant or
    # Postgres for either document -- everything below this point is expected
    # to succeed, so a failure here can't leave the new document half-approved.
    old_doc = _validate_supersede(user, doc, session) if doc.supersedes_document_id else None

    doc.status = "approved"
    doc.reviewed_by_sub = user.sub
    doc.reviewed_at = datetime.now(timezone.utc)

    qdrant_fields = {"status": doc.status}
    if corrections:
        if corrections.classification:
            qdrant_fields["classification"] = doc.classification
        if corrections.releasability is not None:
            qdrant_fields["releasability"] = doc.releasability
        if corrections.access_scope is not None:
            qdrant_fields["access_scope"] = doc.access_scope
    update_document_payload(get_qdrant_client(), str(doc.id), qdrant_fields)

    if old_doc is not None:
        _execute_supersede(user, old_doc, doc, session)

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
    # FR-15: notify the uploader of the decision.
    session.add(
        Notification(
            recipient_sub=doc.uploader_sub,
            document_id=doc.id,
            decision="approved",
            message=f"Your document '{doc.filename}' was approved.",
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
    update_document_payload(get_qdrant_client(), str(doc.id), {"status": doc.status})
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
    # FR-15: notify the uploader of the decision, with the stated reason.
    session.add(
        Notification(
            recipient_sub=doc.uploader_sub,
            document_id=doc.id,
            decision="rejected",
            message=f"Your document '{doc.filename}' was rejected: {body.reason}",
        )
    )
    session.commit()
    session.refresh(doc)
    return doc
