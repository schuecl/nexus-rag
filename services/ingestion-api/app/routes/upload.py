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
import logging
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import metrics
from app.deps import (
    allowed_classifications,
    get_current_user,
    require_ingest,
    require_purge,
    verify_csrf,
)
from app.recovery import mark_published
from common.claims import UserClaims
from common.db import get_session
from common.file_types import (
    SNIFF_BYTES,
    UnsupportedUpload,
    validate_upload,
)
from common.job_queue import publish_ingestion_job
from common.metadata import DocumentMetadataIn, MetadataValidationError, validate_against_claims
from common.models import AuditLogEntry, Document
from common.models import PurgeRequest as PurgeRequestModel
from common.object_store import document_object_key, get_object_store
from common.purge import PurgeError, PurgeRequestError, confirm_purge, purge_document, request_purge
from common.tracing import get_tracer
from common.versioning import SupersedeValidationError, validate_supersede_target

router = APIRouter(prefix="/documents", tags=["ingestion"])
logger = logging.getLogger("ingestion-api")

# #134: spans carry ids, counts, and byte sizes only -- never file content or
# filenames (the purge path treats filenames as content; see common/purge.py).
tracer = get_tracer("ingestion-api")

# #134: spans carry ids, counts, and byte sizes only -- never file content or
# filenames (the purge path treats filenames as content; see common/purge.py).
tracer = get_tracer("ingestion-api")

# FR-9/NFR-7: "a configurable size limit" -- was a hardcoded constant despite
# the comment's own claim; now actually reads from the environment, default
# unchanged (50MB).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))

# How much to pull off the upload at a time in _read_bounded below. Only
# matters as the overshoot allowance on the memory ceiling: peak usage is
# MAX_UPLOAD_BYTES + this, not the size of whatever was sent.
_READ_CHUNK_BYTES = 1024 * 1024

# Issue #279 (gap G3): default true (the safe default -- mirrors COOKIE_SECURE
# below) requires a *different* rag-purge holder to confirm a purge request
# before purge_document ever runs; DELETE /documents/{id} refuses to run
# single-call destruction while this is set. docker-compose.yml explicitly
# sets this false for the dev loop and seed-sample-data, which only ever have
# one purge-capable actor (dave-admin); the Helm chart's production default
# leaves it true. See common/purge.py's request_purge/confirm_purge.
PURGE_TWO_PERSON_REQUIRED = os.environ.get("PURGE_TWO_PERSON_REQUIRED", "true").lower() == "true"
# How long an unconfirmed purge request stays confirmable before it must be
# re-requested -- "so stale ones don't linger as loaded guns" (#279).
PURGE_REQUEST_EXPIRY_HOURS = float(os.environ.get("PURGE_REQUEST_EXPIRY_HOURS", "24"))


async def _read_bounded(file: UploadFile, limit: int) -> bytes:
    """Read at most `limit` bytes, raising 413 the moment that's exceeded.

    This replaces a single `await file.read()` followed by a `len()` check,
    which had to materialise the whole upload in one `bytes` object before it
    could decide the upload was too big -- so a body well over the pod's
    memory limit was an OOM kill of ingestion-api rather than a 413, taking
    the API down for every other user rather than just refusing one request.

    What this does *not* fix, and can't from here: by the time any handler
    runs, Starlette's multipart parser has already consumed the full request
    body, spooling past 1MB (`MultiPartParser.spool_max_size`) into a temp
    file on disk. So this bounds ingestion-api's *memory*, not the bytes
    transferred or written to the container's filesystem. Capping those needs
    a limit at the edge -- see the ingress annotation in
    helm/nexus-rag/values.yaml, and the note in docs/dev-setup.md for the
    Compose stack, which has no proxy in front of this service.
    """
    # The parser has already counted the body, so in practice this is the
    # branch that fires and nothing large is ever copied. The loop below is
    # the guard for the case where size isn't populated.
    if file.size is not None and file.size > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file exceeds size limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit_document(
    request: Request,
    file: UploadFile = File(...),
    classification: str = Form(...),
    releasability: str = Form(..., description="JSON array of strings"),
    access_scope: str = Form(..., description="JSON array of strings"),
    source_originator: str = Form(...),
    doc_type: str = Form(...),
    program_community: str | None = Form(None),
    effective_date: str | None = Form(None),
    supersedes_document_id: str | None = Form(None),
    user: UserClaims = Depends(require_ingest),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    contents = await _read_bounded(file, MAX_UPLOAD_BYTES)
    if not contents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")
    metrics.upload_bytes.observe(len(contents))

    # Issue #211: reject here rather than at parse time. parse_document
    # dispatches on the filename extension, which the uploader chooses, so
    # without this a caller picks which parser runs on their bytes. Rejecting
    # synchronously also means the uploader gets an actionable error now
    # instead of an asynchronous `failed` status minutes later (FR-8).
    try:
        validate_upload(file.filename or "", contents[:SNIFF_BYTES])
    except UnsupportedUpload as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc

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
    #
    # #134: publish inside a named ingest.submit span so the traceparent
    # riding the NATS message headers points here, and ingestion-worker's
    # ingest.process span continues this trace across the queue.
    try:
        with tracer.start_as_current_span(
            "ingest.submit",
            attributes={"document.id": str(doc.id), "document.bytes": len(contents)},
        ):
            await publish_ingestion_job(request.app.state.jetstream, str(doc.id))
        metrics.queue_publish_total.labels(source="request", outcome="acknowledged").inc()
        try:
            mark_published(doc.id)
            doc.queue_published_at = datetime.now(UTC)
            metrics.submissions_total.labels(outcome="published").inc()
        except Exception:
            # JetStream already acknowledged the durable message. Leaving the
            # marker null is safe: the reconciler republishes with the same
            # Nats-Msg-Id, and the worker is duplicate-safe.
            logger.exception(
                "job %s was published but its hand-off marker could not be saved",
                doc.id,
            )
            metrics.submissions_total.labels(outcome="queued_for_recovery").inc()
    except Exception:
        # The document row and original are durable. Return the accepted row
        # and let the background reconciler retry instead of reporting a 5xx
        # that encourages the user to create a second submission.
        logger.exception(
            "initial queue publish failed for document %s; reconciliation will retry",
            doc.id,
        )
        metrics.queue_publish_total.labels(source="request", outcome="failed").inc()
        metrics.submissions_total.labels(outcome="queued_for_recovery").inc()
    return doc


@router.get("/mine")
def list_my_documents(
    user: UserClaims = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Sequence[Document]:
    docs = session.exec(select(Document).where(Document.uploader_sub == user.sub)).all()
    return docs


@router.get("/{doc_id}")
def get_document(
    doc_id: uuid.UUID,
    user: UserClaims = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Document:
    """FR-8: lets a caller poll a submission's status after the immediate
    202 response. Scoped to the uploader themselves -- this isn't a general
    document-lookup endpoint; curators have their own scoped queue view
    (app/routes/curate.py)."""
    doc = session.get(Document, doc_id)
    if doc is None or doc.uploader_sub != user.sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


class PurgeReason(BaseModel):
    reason: str = Field(min_length=1)


@router.delete("/{doc_id}")
def purge(
    doc_id: uuid.UUID,
    body: PurgeReason,
    user: UserClaims = Depends(require_purge),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    """Issue #123: destroy a document's content in every store that holds it.

    The remediation path for classification spillage. Until this existed a
    mis-tagged document could be made unretrievable -- flip its status and the
    FR-26 filter stops matching it -- but never destroyed: the original stayed
    in the object store and the chunks stayed in Qdrant with their text in
    cleartext.

    Irreversible, and deliberately not scoped by ownership or org the way the
    read routes are: a spill is usually discovered by someone other than the
    uploader, and requiring the uploader's cooperation to remediate would be
    the wrong control. Authority comes from the dedicated rag-purge role
    instead (see deps.require_purge for why it is not rag-admin).

    Returns the tombstone: the id and purged status survive so prior audit
    entries still resolve, but every content-bearing field is scrubbed.

    Issue #279 (gap G3): this single-call path only runs when
    PURGE_TWO_PERSON_REQUIRED is unset -- otherwise a lone rag-purge holder
    could destroy a document alone, which is exactly the gap this issue
    closes. When set, use POST {doc_id}/purge-request followed by a
    *different* rag-purge holder's POST .../confirm instead.
    """
    if PURGE_TWO_PERSON_REQUIRED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "two-person purge is required in this deployment: "
            f"POST /documents/{doc_id}/purge-request, then a different rag-purge "
            "holder must confirm it",
        )
    try:
        return purge_document(
            session,
            doc_id,
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            reason=body.reason,
        )
    except PurgeError as exc:
        if "not found" in str(exc):
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        # A store failed mid-purge. The document is already non-retrievable and
        # the operation is idempotent, so the actionable answer is "retry".
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{doc_id}/purge-request")
def create_purge_request(
    doc_id: uuid.UUID,
    body: PurgeReason,
    user: UserClaims = Depends(require_purge),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> PurgeRequestModel:
    """Issue #279 (gap G3): first half of the two-person destruction flow --
    records a purge request row and destroys nothing. A second, different
    rag-purge holder must confirm it (POST .../purge-request/{request_id}/
    confirm) before purge_document ever runs. Available regardless of
    PURGE_TWO_PERSON_REQUIRED, so a deployment can start using the flow
    before flipping the DELETE route off.
    """
    try:
        return request_purge(
            session,
            doc_id,
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            reason=body.reason,
            expiry_hours=PURGE_REQUEST_EXPIRY_HOURS,
        )
    except PurgeRequestError as exc:
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(exc)) from exc


@router.post("/{doc_id}/purge-request/{request_id}/confirm")
def confirm_purge_request(
    doc_id: uuid.UUID,
    request_id: uuid.UUID,
    user: UserClaims = Depends(require_purge),
    session: Session = Depends(get_session),
    _csrf: None = Depends(verify_csrf),
) -> Document:
    """Second half: executes the purge, but only for a rag-purge holder whose
    `sub` differs from the request's own requester -- same-person
    confirmation is refused server-side (common.purge.
    purge_confirmation_authorized), not just discouraged by the UI.
    """
    req = session.get(PurgeRequestModel, request_id)
    if req is None or req.document_id != doc_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "purge request not found")
    try:
        return confirm_purge(
            session,
            request_id,
            actor_sub=user.sub,
            actor_username=user.preferred_username,
        )
    except PurgeRequestError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PurgeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
