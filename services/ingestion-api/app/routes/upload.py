"""FR-1..FR-9: document submission and mandatory tagging. Request handling
(auth, tagging validation, FR-7 supersede-target checks, object-store write)
is synchronous and fast; the actual parse -> chunk -> embed -> store
pipeline (FR-3..FR-6) is handed off to the durable ingestion-worker service
via NATS JetStream (NFR-11) so a slow/large document can't tie up a request
worker, and callers get real queued/processing/embedded/failed progress via
GET /documents/{id} instead of just a pass/fail response.

NFR-11: this used to run the pipeline in-process via FastAPI's
BackgroundTasks, which loses an in-flight document if this process
restarts mid-processing. Publishing to a durable, acked queue instead --
and letting a separate ingestion-worker service actually do the work -- is
what fixes that; see services/ingestion-worker/app/processing.py.
"""

from __future__ import annotations

import json
import os
import uuid

from app.deps import allowed_classifications, get_current_user, require_ingest, verify_csrf
from common.db import get_session
from common.job_queue import publish_ingestion_job
from common.metadata import DocumentMetadataIn, MetadataValidationError, validate_against_claims
from common.models import AuditLogEntry, Document
from common.object_store import document_object_key, get_object_store
from common.versioning import SupersedeValidationError, validate_supersede_target
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlmodel import Session, select

router = APIRouter(prefix="/documents", tags=["ingestion"])

# FR-9/NFR-7: "a configurable size limit" -- was a hardcoded constant despite
# the comment's own claim; now actually reads from the environment, default
# unchanged (50MB).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit_document(
    request: Request,
    file: UploadFile = File(...),
    classification: str = Form(...),
    # FR-20/Section 6.3: a single value, unlike access_scope below.
    releasability: str = Form(...),
    access_scope: str = Form(..., description="JSON array of strings"),
    source_originator: str = Form(...),
    doc_type: str = Form(...),
    program_community: str | None = Form(None),
    effective_date: str | None = Form(None),
    supersedes_document_id: str | None = Form(None),
    user=Depends(require_ingest),
    session: Session = Depends(get_session),
    _csrf=Depends(verify_csrf),
):
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file exceeds size limit")
    if not contents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    try:
        metadata = DocumentMetadataIn(
            classification=classification,
            releasability=releasability,
            access_scope=json.loads(access_scope),
            source_originator=source_originator,
            doc_type=doc_type,
            program_community=program_community,
            effective_date=effective_date,
            supersedes_document_id=supersedes_document_id,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid metadata: {exc}") from exc

    allowed = allowed_classifications(session, user.clearance)
    try:
        validate_against_claims(
            metadata,
            allowed_classifications=allowed,
            user_releasability=user.releasability,
        )
    except MetadataValidationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "; ".join(exc.errors)) from exc

    # FR-7: if this submission claims to be a new version of an existing
    # document, re-validate the target server-side -- not just that it
    # exists, but that this uploader is actually authorized to act on it.
    superseded_doc: Document | None = None
    if metadata.supersedes_document_id:
        try:
            target_id = uuid.UUID(metadata.supersedes_document_id)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "supersedes_document_id is not a valid UUID"
            ) from exc
        superseded_doc = session.get(Document, target_id)
        if superseded_doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "supersedes_document_id not found")
        try:
            validate_supersede_target(
                superseded_doc,
                new_owner_org=user.org or "unknown",
                allowed_classifications=allowed,
                user_releasability=user.releasability,
            )
        except SupersedeValidationError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "; ".join(exc.errors)) from exc

    doc = Document(
        filename=file.filename or "unnamed",
        uploader_sub=user.sub,
        uploader_username=user.preferred_username,
        owner_org=user.org or "unknown",
        classification=metadata.classification,
        releasability=metadata.releasability,
        access_scope=metadata.access_scope,
        source_originator=metadata.source_originator,
        doc_type=metadata.doc_type,
        program_community=metadata.program_community,
        effective_date=metadata.effective_date,
        status="queued",
        supersedes_document_id=superseded_doc.id if superseded_doc else None,
    )
    # NFR-12: durably store the original before returning 202 -- doc.id is
    # already populated (Document.id's default_factory runs at construction,
    # not at commit), so the key is available immediately.
    doc.original_object_key = document_object_key(doc.id)
    get_object_store().put(doc.original_object_key, contents)

    session.add(doc)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.submit",
            target_id=str(doc.id),
            detail={
                "filename": doc.filename,
                "classification": doc.classification,
                "supersedes_document_id": str(doc.supersedes_document_id)
                if doc.supersedes_document_id
                else None,
            },
        )
    )
    session.commit()
    session.refresh(doc)

    # NFR-11: hand off to the durable queue -- ingestion-worker (a separate
    # process/pod) does the actual parse/chunk/embed/store pipeline and
    # drives doc.status through processing -> embedded -> pending_review (or
    # failed). request.app.state.jetstream is set up once at startup
    # (app/main.py's lifespan), not reconnected per request.
    await publish_ingestion_job(request.app.state.jetstream, str(doc.id))
    return doc


@router.get("/mine")
def list_my_documents(
    user=Depends(get_current_user),
    session: Session = Depends(get_session),
):
    docs = session.exec(select(Document).where(Document.uploader_sub == user.sub)).all()
    return docs


@router.get("/{doc_id}")
def get_document(
    doc_id: uuid.UUID,
    user=Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """FR-8: lets a caller poll a submission's status after the immediate
    202 response. Scoped to the uploader themselves -- this isn't a general
    document-lookup endpoint; curators have their own scoped queue view
    (app/routes/curate.py)."""
    doc = session.get(Document, doc_id)
    if doc is None or doc.uploader_sub != user.sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc
