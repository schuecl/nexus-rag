"""FR-1..FR-9: document submission and mandatory tagging. Request handling
(auth, tagging validation, FR-7 supersede-target checks) is synchronous and
fast; the actual parse -> chunk -> embed -> store pipeline (FR-3..FR-6) runs
in the background (FR-8) so a slow/large document can't tie up a request
worker, and callers get real queued/processing/embedded/failed progress via
GET /documents/{id} instead of just a pass/fail response.
"""

from __future__ import annotations

import json
import os
import uuid

from app.chunking import chunk_sections
from app.deps import allowed_classifications, get_current_user, require_ingest
from app.embedding import EmbeddingError, embed_texts
from app.parsing import ParsingError, parse_document
from common.db import get_engine, get_session
from common.metadata import DocumentMetadataIn, MetadataValidationError, validate_against_claims
from common.models import AuditLogEntry, Document
from common.qdrant_store import chunk_vector, ensure_collection, get_qdrant_client, upsert_chunks
from common.sparse_embedding import embed_sparse
from common.versioning import SupersedeValidationError, validate_supersede_target
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from qdrant_client.models import PointStruct
from sqlmodel import Session, select

router = APIRouter(prefix="/documents", tags=["ingestion"])

# FR-9/NFR-7: "a configurable size limit" -- was a hardcoded constant despite
# the comment's own claim; now actually reads from the environment, default
# unchanged (50MB).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))


async def _process_document(document_id: uuid.UUID, filename: str, contents: bytes) -> None:
    """FR-3..FR-6, run after the request that created the `queued` row has
    already returned. Uses its own DB session -- the request-scoped one is
    gone by the time a background task runs. Any failure lands the document
    in `failed` with a message in processing_error rather than propagating
    (NFR-7: malformed input must not crash the worker); there's no HTTP
    response left here to attach an error to.
    """
    with Session(get_engine()) as session:
        doc = session.get(Document, document_id)
        if doc is None:
            return  # shouldn't happen; nothing sensible to do if it did

        doc.status = "processing"
        session.add(doc)
        session.commit()

        try:
            sections = parse_document(filename, contents)
            chunks = chunk_sections(sections)
            if not chunks:
                raise ParsingError("document contained no extractable text")

            dense_vectors = await embed_texts([c.text for c in chunks])
            sparse_vectors = embed_sparse([c.text for c in chunks])

            points = [
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=chunk_vector(dense, sparse),
                    payload={
                        "document_id": str(doc.id),
                        "chunk_index": chunk.chunk_index,
                        "text": chunk.text,
                        "heading": chunk.heading,
                        "page_or_slide": chunk.page_or_slide,
                        "filename": doc.filename,
                        "doc_type": doc.doc_type,
                        "source_originator": doc.source_originator,
                        "classification": doc.classification,
                        "releasability": doc.releasability,
                        "access_scope": doc.access_scope,
                        # Written as pending_review directly (not doc.status,
                        # which is still `processing` at this point) -- this
                        # is what keeps the chunk excluded from retrieval
                        # (FR-11/FR-26) until a curator approves it.
                        "status": "pending_review",
                    },
                )
                for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors)
            ]
            qdrant = get_qdrant_client()
            ensure_collection(qdrant, dense_size=len(dense_vectors[0]))
            upsert_chunks(qdrant, points)

            doc.status = "embedded"
            doc.chunk_count = len(chunks)
            session.add(doc)
            session.commit()

            doc.status = "pending_review"
            session.add(doc)
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.embedded",
                    target_id=str(doc.id),
                    detail={"filename": doc.filename, "chunk_count": doc.chunk_count},
                )
            )
            session.commit()
        except (ParsingError, EmbeddingError) as exc:
            doc.status = "failed"
            doc.processing_error = str(exc)
            session.add(doc)
            session.add(
                AuditLogEntry(
                    actor_sub=doc.uploader_sub,
                    actor_username=doc.uploader_username,
                    action="document.failed",
                    target_id=str(doc.id),
                    detail={"error": str(exc)},
                )
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 -- NFR-7: never crash the worker
            doc.status = "failed"
            doc.processing_error = f"unexpected error: {exc}"
            session.add(doc)
            session.commit()


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    classification: str = Form(...),
    releasability: str = Form(..., description="JSON array of strings"),
    access_scope: str = Form(..., description="JSON array of strings"),
    source_originator: str = Form(...),
    doc_type: str = Form(...),
    program_community: str | None = Form(None),
    effective_date: str | None = Form(None),
    supersedes_document_id: str | None = Form(None),
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

    background_tasks.add_task(_process_document, doc.id, doc.filename, contents)
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
