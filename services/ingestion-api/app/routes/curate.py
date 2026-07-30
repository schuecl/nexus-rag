"""FR-10..FR-16: curation queue, scoped to the orgs a curator holds authority
for (FR-12), with approval capped by org (FR-14.2) and by clearance/releasability
(FR-14.1, mirroring FR-18's uploader-side check).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import Session, select

from app.deps import allowed_classifications, require_curator, verify_csrf
from common.claims import UserClaims
from common.db import get_session
from common.metadata import releasability_authorized
from common.models import AuditLogEntry, Document, Notification
from common.vector_store import get_store

logger = logging.getLogger("ingestion-api")

router = APIRouter(prefix="/curate", tags=["curation"])


@router.get("/queue")
def list_queue(
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
) -> Sequence[Document]:
    docs = session.exec(
        select(Document)
        .where(Document.status == "pending_review")
        .where(Document.owner_org.in_(user.curatable_orgs))  # type: ignore[attr-defined]
    ).all()
    return docs


# Issue #266: statuses a document can be edited from through the curation
# "List" dashboard below. Deliberately excludes the in-flight pipeline states
# (queued/processing/embedded -- the worker owns the row until it reaches
# pending_review) and the terminal/destroyed ones (superseded/purging/purged),
# where editing metadata would either race the worker or mutate a record that
# is supposed to be frozen or already gone.
EDITABLE_STATUSES = {"pending_review", "approved", "rejected"}


@router.get("/documents")
def list_documents(
    status_filter: str | None = Query(None, alias="status"),
    classification: str | None = None,
    q: str | None = Query(
        None,
        description="Case-insensitive search across filename, "
        "source/originator, document type, and uploader username",
    ),
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
) -> Sequence[Document]:
    """Issue #266: the "master list" -- every document (any status) the
    curator holds authority over, not just the pending_review queue
    /curate/queue above returns. Scoped identically (owner_org in
    curatable_orgs); the filters below only ever narrow that set further,
    never widen it.
    """
    stmt = select(Document).where(Document.owner_org.in_(user.curatable_orgs))  # type: ignore[attr-defined]
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)
    if classification:
        stmt = stmt.where(Document.classification == classification)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(
            or_(
                Document.filename.ilike(needle),  # type: ignore[attr-defined]
                Document.source_originator.ilike(needle),  # type: ignore[attr-defined]
                Document.doc_type.ilike(needle),  # type: ignore[attr-defined]
                Document.uploader_username.ilike(needle),  # type: ignore[attr-defined]
            )
        )
    return session.exec(stmt.order_by(Document.updated_at.desc())).all()  # type: ignore[attr-defined]


class DocumentEdit(BaseModel):
    classification: str | None = None
    # FR-20/Section 6.3: "one or more" cardinality, same as DocumentMetadataIn
    # at upload time -- an edit is not the place to relax that constraint, so
    # an explicit empty list is rejected (422) rather than silently orphaning
    # the document (no releasability caveat/no access scope at all).
    releasability: list[str] | None = Field(default=None, min_length=1)
    access_scope: list[str] | None = Field(default=None, min_length=1)
    source_originator: str | None = None
    doc_type: str | None = None
    program_community: str | None = None
    effective_date: str | None = None


@router.patch("/documents/{doc_id}")
def edit_metadata(
    doc_id: uuid.UUID,
    body: DocumentEdit,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    """Issue #266: lets a curator correct a document's metadata after it has
    already cleared curation, without going through supersession -- the gap
    the issue calls out ("no mechanism to affect the metadata of an ingested
    file after ingestion"). Same authority model as approve()/reject(): a 404
    for a document outside the caller's curatable orgs (no existence oracle,
    same reasoning as _check_curator_authority), and a re-check against the
    *edited* classification/releasability so a curator can't use this to
    approve-adjacent their way past their own clearance/releasability ceiling.

    Deletion is deliberately NOT handled here -- that is the separate,
    rag-purge-gated DELETE /documents/{id} in app/routes/upload.py
    (common/purge.py). Purge is intentionally not curator authority (see
    app/deps.require_purge), so the curation list page calls that existing
    endpoint directly rather than this module growing a second, weaker path
    to the same destructive action.
    """
    doc = session.get(Document, doc_id, with_for_update=True)
    if doc is None or not user.can_curate_org(doc.owner_org):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"document is '{doc.status}' and cannot be edited from the curation list -- "
            f"only {', '.join(sorted(EDITABLE_STATUSES))} documents can be",
        )
    _check_curator_authority(user, doc, session)

    before = {
        "classification": doc.classification,
        "releasability": list(doc.releasability),
        "access_scope": list(doc.access_scope),
    }
    changed_qdrant: dict[str, str | list[str]] = {}

    if body.classification is not None and body.classification != doc.classification:
        doc.classification = body.classification
        changed_qdrant["classification"] = doc.classification
    if body.releasability is not None and body.releasability != doc.releasability:
        doc.releasability = body.releasability
        changed_qdrant["releasability"] = doc.releasability
    if body.access_scope is not None and body.access_scope != doc.access_scope:
        doc.access_scope = body.access_scope
        changed_qdrant["access_scope"] = doc.access_scope
    if body.source_originator is not None:
        doc.source_originator = body.source_originator
    if body.doc_type is not None:
        doc.doc_type = body.doc_type
    if body.program_community is not None:
        doc.program_community = body.program_community or None
    if body.effective_date is not None:
        doc.effective_date = body.effective_date or None

    # Re-check authority against the *edited* tags, not just what the document
    # already carried -- same reasoning as approve()'s post-correction re-check.
    if "classification" in changed_qdrant or "releasability" in changed_qdrant:
        _check_curator_authority(user, doc, session)

    doc.updated_at = datetime.now(UTC)

    if not changed_qdrant:
        # Nothing that Qdrant also holds a copy of changed -- e.g. only
        # source_originator/doc_type/program_community/effective_date edited.
        # No access-control-relevant write, so no NFR-13 revert dance needed.
        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub=user.sub,
                actor_username=user.preferred_username,
                action="document.metadata_edit",
                target_id=str(doc.id),
                detail={"fields": list(body.model_dump(exclude_none=True))},
            )
        )
        session.commit()
        session.refresh(doc)
        return doc

    store = get_store()
    store.update_document_payload(str(doc.id), changed_qdrant)
    # NFR-13: same reasoning as approve()/reject() -- best-effort revert the
    # Qdrant write if the Postgres commit doesn't durably land, so the two
    # stores don't end up disagreeing about this document's access-control
    # fields.
    try:
        session.add(doc)
        session.add(
            AuditLogEntry(
                actor_sub=user.sub,
                actor_username=user.preferred_username,
                action="document.metadata_edit",
                target_id=str(doc.id),
                detail={"fields": list(body.model_dump(exclude_none=True))},
            )
        )
        session.commit()
    except Exception:
        try:
            store.update_document_payload(str(doc.id), {k: before[k] for k in changed_qdrant})
        except Exception:
            logger.exception(
                "metadata edit of document %s failed and the Qdrant revert also "
                "failed -- its payload may not match Postgres; needs manual "
                "reconciliation",
                doc.id,
            )
        raise
    session.refresh(doc)
    return doc


def _load_pending(session: Session, doc_id: uuid.UUID, *, lock: bool = False) -> Document:
    # Issue #215: `lock=True` takes a row lock (SELECT ... FOR UPDATE) so two
    # curators acting on the same document can't both pass the
    # `pending_review` check and both proceed. Same mechanism #164 introduced
    # for the worker's processing lease. Not taken on read-only paths, which
    # would otherwise serialise the queue view against every decision.
    doc = session.get(Document, doc_id, with_for_update=lock)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc.status != "pending_review":
        raise HTTPException(status.HTTP_409_CONFLICT, f"document is already {doc.status}")
    return doc


def _check_curator_authority(user: UserClaims, doc: Document, session: Session) -> None:
    if not user.can_curate_org(doc.owner_org):
        # Issue #215: 404, not 403. A 403 naming the owning org told a curator
        # scoped to one org that a given document id exists and which org owns
        # it -- an existence oracle, and one that leaks an org name to someone
        # with no authority over it. To a caller who may not curate this
        # document, it is indistinguishable from a document that isn't there,
        # which is exactly what they should be able to observe.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    allowed = allowed_classifications(session, user.clearance)
    if doc.classification not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cannot approve a document above your own cleared level",
        )
    # FR-14.1: "by clearance/releasability, same as FR-18" -- a curator who
    # doesn't hold one of the document's releasability values can't publish
    # it, mirroring validate_against_claims' uploader-side check exactly.
    if not releasability_authorized(doc.releasability, user.releasability):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"cannot approve a document with releasability values {doc.releasability}, "
            "one or more of which you do not hold",
        )


def _validate_supersede(user: UserClaims, new_doc: Document, session: Session) -> Document:
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


def _execute_supersede(
    user: UserClaims, old_doc: Document, new_doc: Document, session: Session
) -> None:
    """The actual swap -- only called after _validate_supersede has already
    passed, so this is expected not to fail."""
    # #160: through the backend seam (Qdrant by default, Milvus opt-in).
    get_store().delete_document_chunks(str(old_doc.id))
    old_doc.status = "superseded"
    old_doc.updated_at = datetime.now(UTC)
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
    releasability: list[str] | None = None  # FR-20/Section 6.3: one or more values
    access_scope: list[str] | None = None


@router.post("/{doc_id}/approve")
def approve(
    doc_id: uuid.UUID,
    corrections: Corrections | None = None,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    doc = _load_pending(session, doc_id, lock=True)
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
    doc.reviewed_at = datetime.now(UTC)

    qdrant_fields: dict[str, str | list[str]] = {"status": doc.status}
    if corrections:
        if corrections.classification:
            qdrant_fields["classification"] = doc.classification
        if corrections.releasability is not None:
            qdrant_fields["releasability"] = doc.releasability
        if corrections.access_scope is not None:
            qdrant_fields["access_scope"] = doc.access_scope
    store = get_store()
    store.update_document_payload(str(doc.id), qdrant_fields)

    # NFR-13: Qdrant now already shows the new document as `approved`, but
    # nothing is durable yet -- Postgres (the system of record for the
    # curation queue and the uploader-facing status) only reflects that once
    # session.commit() below succeeds. If anything in this block raises (a DB
    # error, the old document's Qdrant delete failing, etc.), the Postgres
    # transaction rolls back on its own (get_session's context manager), but
    # the Qdrant write above doesn't -- so best-effort revert it too, keeping
    # both stores agreeing again on `pending_review` rather than leaving a
    # document Postgres still lists as pending already showing as approved
    # (and therefore retrievable, FR-11/FR-26) in Qdrant.
    try:
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
    except Exception:
        try:
            store.update_document_payload(str(doc.id), {"status": "pending_review"})
        except Exception:
            logger.exception(
                "approval of document %s failed and the Qdrant status revert also "
                "failed -- its chunks may still show status=approved despite Postgres "
                "still saying pending_review; needs manual reconciliation",
                doc.id,
            )
        raise
    session.refresh(doc)
    return doc


class Rejection(BaseModel):
    reason: str


@router.post("/{doc_id}/reject")
def reject(
    doc_id: uuid.UUID,
    body: Rejection,
    user: UserClaims = Depends(require_curator),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    doc = _load_pending(session, doc_id, lock=True)
    _check_curator_authority(user, doc, session)

    doc.status = "rejected"
    doc.rejection_reason = body.reason
    doc.reviewed_by_sub = user.sub
    doc.reviewed_at = datetime.now(UTC)
    store = get_store()
    store.update_document_payload(str(doc.id), {"status": doc.status})
    # NFR-13: same reasoning as approve() above -- revert the Qdrant write if
    # the Postgres commit doesn't durably land, so the two stores don't end up
    # disagreeing about whether this document is still pending.
    try:
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
    except Exception:
        try:
            store.update_document_payload(str(doc.id), {"status": "pending_review"})
        except Exception:
            logger.exception(
                "rejection of document %s failed and the Qdrant status revert also "
                "failed -- its chunks may still show status=rejected despite Postgres "
                "still saying pending_review; needs manual reconciliation",
                doc.id,
            )
        raise
    session.refresh(doc)
    return doc
