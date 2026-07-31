"""Issue #123: destruction of a document across every store that holds it.

Until now nothing could be removed. `delete_document_chunks()` was reachable
only via the FR-7 supersede path, `ObjectStore.delete()` had no callers at all,
and there was no delete route anywhere -- so a mis-classified document that
reached `approved` could be made non-retrievable (flip its status, and the
FR-26 filter stops matching it) but never actually destroyed. In a deployment
where spillage remediation is a defined procedure, "the bytes are still there"
is the part that matters.

Ordering is chosen so a partial failure always leaves the document *less*
exposed, never more, and so the whole operation is safely retryable:

  1. status -> "purging", committed first. The FR-26 filter requires
     `approved`, so this alone makes the document unretrievable. If everything
     after this fails, the document is already inert and a retry resumes.
  2. Qdrant chunks deleted -- removes the text from the vector store, which is
     the copy most widely reachable (see #110: nothing at the network layer
     restricted who could read it, and payloads hold cleartext).
  3. Object-store original deleted.
  4. Postgres row tombstoned and the audit entry written, in one transaction.

Each step is idempotent: deleting already-absent chunks or a missing object is
a no-op rather than an error, so re-running a half-finished purge converges.

The row is tombstoned rather than hard-deleted. A tombstone keeps the id, so
prior audit entries referencing this document still resolve to something and
the destruction itself is provable -- but every field that could carry
classified content is scrubbed, because retaining `filename` on a document
purged *for* a filename-bearing spill would defeat the point. What survives is
the fact of the document, not its content.

Deliberately NOT handled here: expunging prior audit entries that mention this
document. NFR-2 makes audit_log append-only on purpose and the application role
holds INSERT and nothing else since #278 -- not even SELECT -- so it *cannot*
delete them by construction. That
tension is real and is called out in #123 -- resolving it needs a policy
decision and an out-of-band administrative path, not an application function.

Issue #279 (gap G3): `purge_document` above is a complete, irreversible
destruction in one call -- fine as a primitive, but it means a single
`rag-purge` holder can act alone. `request_purge`/`confirm_purge` below add a
two-person gate in front of it: a request records intent and destroys
nothing, and a *different* `rag-purge` holder must confirm before
`purge_document` ever runs (`purge_confirmation_authorized` is the actual
invariant -- confirming_sub != requested_by_sub). Whether this gate is
mandatory is a deployment decision, not this module's: see
`PURGE_TWO_PERSON_REQUIRED` in `services/ingestion-api/app/routes/upload.py`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from .log_safety import log_safe
from .models import AuditLogEntry, Document, PurgeRequest
from .object_store import get_object_store
from .vector_store import get_store

logger = logging.getLogger("purge")

PURGED_STATUS = "purged"
PURGING_STATUS = "purging"

PENDING_STATUS = "pending"
CONFIRMED_STATUS = "confirmed"

# What a tombstone keeps: id, status, timestamps, and the purge record. Every
# other field is scrubbed to this marker so nothing about the destroyed
# document's content, origin, or tagging survives in a queryable row.
SCRUBBED = "[purged]"


class PurgeError(Exception):
    pass


class PurgeRequestError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware_utc(dt: datetime) -> datetime:
    """Some drivers/dialects round-trip a tz-aware datetime as naive (sqlite
    always does; a plain, non-timezone(True) column can too) -- treat a naive
    value read back from the DB as UTC rather than letting `expires_at <=
    _utcnow()` raise on offset-naive vs. offset-aware. Same helper as
    ingestion-api/app/deps.py's `_as_aware_utc` for `UserSession.expires_at`;
    duplicated rather than imported since `common` must not depend on an
    app-layer package."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def purge_document(
    session: Session,
    document_id: uuid.UUID,
    *,
    actor_sub: str,
    actor_username: str,
    reason: str,
    requested_by_sub: str | None = None,
    requested_by_username: str | None = None,
) -> Document:
    """Destroy a document's content everywhere and tombstone its row.

    `reason` is required rather than optional: a purge is an irreversible
    administrative action, and "why" is the first thing an accreditation review
    asks. It is recorded in the audit entry.

    `requested_by_sub`/`requested_by_username` are set only when this call is
    the confirmation half of the #279 two-person flow below -- `actor_sub` is
    already the *confirming* identity (the one whose authority actually runs
    this function), so the requester's identity would otherwise be lost from
    the audit trail entirely.
    """
    doc = session.get(Document, document_id)
    if doc is None:
        raise PurgeError(f"document {document_id} not found")
    if doc.status == PURGED_STATUS:
        # Idempotent: a retry of a completed purge is a success, not an error.
        return doc

    original_status = doc.status
    chunk_count = doc.chunk_count
    object_key = doc.original_object_key
    # Captured before any mutation: step 4 below scrubs doc.classification,
    # and issue #229 needs the pre-scrub value to find the right collection.
    classification = doc.classification

    # 1. Make it unretrievable before destroying anything. Committed on its own
    #    so a failure below cannot leave the document still matchable.
    doc.status = PURGING_STATUS
    doc.updated_at = _utcnow()
    session.add(doc)
    session.commit()

    # 2. Vector store. Idempotent by filter -- deleting no-longer-present
    #    chunks succeeds.
    try:
        get_store().delete_document_chunks(str(doc.id), classification)
    except Exception as exc:
        raise PurgeError(
            f"could not delete chunks for {document_id}: {exc}. The document is "
            f"already marked '{PURGING_STATUS}' and is not retrievable; retry to finish."
        ) from exc

    # 3. Original file. missing_ok on the filesystem backend, and a missing S3
    #    key is not an error either, so a partial retry converges.
    if object_key:
        try:
            get_object_store().delete(object_key)
        except FileNotFoundError:
            logger.info(
                "original for %s already absent from the object store", log_safe(document_id)
            )
        except Exception as exc:
            raise PurgeError(
                f"could not delete the original for {document_id}: {exc}. Chunks are "
                f"already destroyed and the document is not retrievable; retry to finish."
            ) from exc

    # 4. Tombstone + audit, atomically.
    doc.status = PURGED_STATUS
    doc.filename = SCRUBBED
    doc.source_originator = SCRUBBED
    doc.doc_type = SCRUBBED
    doc.classification = SCRUBBED
    doc.releasability = []
    doc.access_scope = []
    doc.program_community = None
    doc.effective_date = None
    doc.rejection_reason = None
    doc.processing_error = None
    doc.original_object_key = None
    doc.chunk_count = 0
    doc.updated_at = _utcnow()
    session.add(doc)
    session.add(
        AuditLogEntry(
            actor_sub=actor_sub,
            actor_username=actor_username,
            action="document.purged",
            target_id=str(doc.id),
            # No filename: this entry outlives the document by design (NFR-2),
            # so putting the destroyed document's name in it would leave the
            # very content a spillage purge exists to remove.
            detail={
                "reason": reason,
                "status_before_purge": original_status,
                "chunks_destroyed": chunk_count,
                "original_destroyed": object_key is not None,
                # #279: both identities land in this one row when a
                # two-person request/confirm preceded this call -- actor_sub
                # above is already the confirmer.
                **(
                    {
                        "requested_by_sub": requested_by_sub,
                        "requested_by_username": requested_by_username,
                    }
                    if requested_by_sub is not None
                    else {}
                ),
            },
        )
    )
    session.commit()
    session.refresh(doc)
    # log_safe on the externally-sourced values: actor_username is an OIDC
    # claim, so a newline in it could forge an entire log line (CodeQL
    # flagged this). document_id is uuid.UUID-typed and cannot contain one,
    # but escaping it costs nothing and saves the next reader deciding which
    # arguments need it.
    logger.warning(
        "document %s purged by %s (was %s, %d chunks)",
        log_safe(document_id),
        log_safe(actor_username),
        log_safe(original_status),
        chunk_count,
    )
    return doc


def purge_confirmation_authorized(*, requested_by_sub: str, confirming_sub: str) -> bool:
    """Issue #279 (gap G3): the two-person invariant itself, pulled out as its
    own predicate -- mirroring `common.metadata.access_scope_authorized` --
    so it is a security invariant a BDD scenario can pin directly rather than
    an inline comparison buried inside `confirm_purge`."""
    return confirming_sub != requested_by_sub


def request_purge(
    session: Session,
    document_id: uuid.UUID,
    *,
    actor_sub: str,
    actor_username: str,
    reason: str,
    expiry_hours: float,
) -> PurgeRequest:
    """First half of the two-person flow: records intent, destroys nothing.
    A second, different `rag-purge` holder must call `confirm_purge` with the
    returned request's id before anything actually happens.
    """
    doc = session.get(Document, document_id)
    if doc is None:
        raise PurgeRequestError(f"document {document_id} not found")
    if doc.status == PURGED_STATUS:
        raise PurgeRequestError(f"document {document_id} is already purged")

    now = _utcnow()
    existing = session.exec(
        select(PurgeRequest).where(
            PurgeRequest.document_id == document_id,
            PurgeRequest.status == PENDING_STATUS,
        )
    ).all()
    if any(_as_aware_utc(r.expires_at) > now for r in existing):
        raise PurgeRequestError(
            f"document {document_id} already has an unexpired pending purge request"
        )

    req = PurgeRequest(
        document_id=document_id,
        reason=reason,
        requested_by_sub=actor_sub,
        requested_by_username=actor_username,
        expires_at=now + timedelta(hours=expiry_hours),
    )
    session.add(req)
    session.add(
        AuditLogEntry(
            actor_sub=actor_sub,
            actor_username=actor_username,
            action="document.purge_requested",
            target_id=str(document_id),
            detail={"reason": reason, "purge_request_id": str(req.id)},
        )
    )
    session.commit()
    session.refresh(req)
    return req


def confirm_purge(
    session: Session,
    request_id: uuid.UUID,
    *,
    actor_sub: str,
    actor_username: str,
) -> Document:
    """Second half: a different `rag-purge` holder confirms, which is what
    actually runs `purge_document`. Refuses a same-actor confirmation and an
    expired request; anything else (a store failure mid-`purge_document`) is
    safely retryable by confirming again, exactly like `purge_document`
    itself -- this only flips the request to `confirmed` once that call has
    actually succeeded, so a partial failure leaves it `pending` and
    re-confirmable rather than stuck.
    """
    req = session.get(PurgeRequest, request_id)
    if req is None:
        raise PurgeRequestError(f"purge request {request_id} not found")
    if req.status == CONFIRMED_STATUS:
        doc = session.get(Document, req.document_id)
        if doc is None:
            raise PurgeRequestError(f"document {req.document_id} not found")
        return doc
    if _as_aware_utc(req.expires_at) <= _utcnow():
        raise PurgeRequestError(f"purge request {request_id} has expired; submit a new request")
    if not purge_confirmation_authorized(
        requested_by_sub=req.requested_by_sub, confirming_sub=actor_sub
    ):
        raise PurgeRequestError(
            "a purge request must be confirmed by a different rag-purge holder "
            "than the one who filed it"
        )

    doc = purge_document(
        session,
        req.document_id,
        actor_sub=actor_sub,
        actor_username=actor_username,
        reason=req.reason,
        requested_by_sub=req.requested_by_sub,
        requested_by_username=req.requested_by_username,
    )

    req.status = CONFIRMED_STATUS
    req.confirmed_by_sub = actor_sub
    req.confirmed_by_username = actor_username
    req.confirmed_at = _utcnow()
    session.add(req)
    session.commit()
    return doc
