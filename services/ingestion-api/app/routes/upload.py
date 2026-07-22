"""FR-1..FR-9: document submission and mandatory tagging, followed by the
parse -> chunk -> embed -> store pipeline (FR-3..FR-6). Processing is
synchronous within the request -- fine for the small documents this dev stack
is meant to exercise, but a real deployment would move this to a background
worker so FR-8's queued/processing states mean something; right now a
submission either fully succeeds (pending_review, chunks embedded and stored)
or fails outright (FR-9), there's no in-between state.
"""

from __future__ import annotations

import json
import uuid

from app.chunking import chunk_sections
from app.deps import allowed_classifications, get_current_user, require_ingest
from app.embedding import EmbeddingError, embed_texts
from app.parsing import ParsingError, parse_document
from common.db import get_session
from common.metadata import DocumentMetadataIn, MetadataValidationError, validate_against_claims
from common.models import AuditLogEntry, Document
from common.qdrant_store import chunk_vector, ensure_collection, get_qdrant_client, upsert_chunks
from common.sparse_embedding import embed_sparse
from common.versioning import SupersedeValidationError, validate_supersede_target
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from qdrant_client.models import PointStruct
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

    # FR-3/FR-4: parse into structural sections, then chunk within them.
    try:
        sections = parse_document(file.filename or "unnamed", contents)
    except ParsingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    chunks = chunk_sections(sections)
    if not chunks:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "document contained no extractable text"
        )

    # FR-5: embed every chunk -- dense for semantic search, sparse BM25 for
    # keyword search (FR-24). Both are needed at query time to fuse.
    try:
        dense_vectors = await embed_texts([c.text for c in chunks])
    except EmbeddingError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    sparse_vectors = embed_sparse([c.text for c in chunks])

    # Document.id is populated by its default_factory at construction time, so
    # it's available for the Qdrant payload before this row is ever committed.
    # Nothing is persisted to Postgres until the Qdrant write below succeeds --
    # a best-effort ordering, not a real cross-store transaction.
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
        chunk_count=len(chunks),
        supersedes_document_id=superseded_doc.id if superseded_doc else None,
    )

    # FR-6: store each chunk's vector and full metadata payload, including the
    # pending_review status that keeps it excluded from retrieval (FR-11/FR-26)
    # until a curator approves it.
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
                "status": doc.status,
            },
        )
        for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors)
    ]
    qdrant = get_qdrant_client()
    ensure_collection(qdrant, dense_size=len(dense_vectors[0]))
    upsert_chunks(qdrant, points)

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
                "chunk_count": doc.chunk_count,
                "supersedes_document_id": str(doc.supersedes_document_id)
                if doc.supersedes_document_id
                else None,
            },
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
