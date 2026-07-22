"""FR-1..FR-9: document submission and mandatory tagging. Parsing/chunking/
embedding (FR-3..FR-6) are deferred -- see TODO below -- this route's job for
now is proving the tagging-enforcement and pending-review-state plumbing works.
"""

from __future__ import annotations

import json

from app.deps import allowed_classifications, get_current_user, require_ingest
from common.db import get_session
from common.metadata import DocumentMetadataIn, MetadataValidationError, validate_against_claims
from common.models import AuditLogEntry, Document
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlmodel import Session, select

router = APIRouter(prefix="/documents", tags=["ingestion"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # NFR-7 configurable size guard


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_document(
    file: UploadFile = File(...),
    classification: str = Form(...),
    releasability: str = Form(..., description="JSON array of strings"),
    access_scope: str = Form(..., description="JSON array of strings"),
    source_originator: str = Form(...),
    doc_type: str = Form(...),
    program_community: str | None = Form(None),
    effective_date: str | None = Form(None),
    user=Depends(require_ingest),
    session: Session = Depends(get_session),
):
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file exceeds size limit")
    if not contents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    try:
        metadata = DocumentMetadataIn(
            classification=classification,
            releasability=json.loads(releasability),
            access_scope=json.loads(access_scope),
            source_originator=source_originator,
            doc_type=doc_type,
            program_community=program_community,
            effective_date=effective_date,
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

    # TODO(FR-3..FR-6): parse -> chunk -> embed -> write vectors+payload to Qdrant.
    # For now the raw bytes are discarded; only the tagged, pending_review record
    # is persisted so curation (Section 4.2) has something to act on.
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
        status="pending_review",
    )
    session.add(doc)
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.submit",
            target_id=str(doc.id),
            detail={"filename": doc.filename, "classification": doc.classification},
        )
    )
    session.commit()
    session.refresh(doc)
    return doc


@router.get("/mine")
def list_my_documents(
    user=Depends(get_current_user),
    session: Session = Depends(get_session),
):
    docs = session.exec(select(Document).where(Document.uploader_sub == user.sub)).all()
    return docs
