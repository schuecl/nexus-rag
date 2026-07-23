"""Postgres tables: system of record for document status, audit log, and the
admin-configurable Classification/Releasability lists (C9). Qdrant remains the
vector store; these tables are the transactional source of truth (see plan notes
in REQUIREMENTS.md Section 6.3 and C9)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, JSON


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClassificationLevel(SQLModel, table=True):
    """Admin-configurable, ranked list (C9). Lower rank = less sensitive."""

    __tablename__ = "classification_levels"

    id: int | None = Field(default=None, primary_key=True)
    value: str = Field(unique=True, index=True)
    rank: int
    active: bool = Field(default=True)


class ReleasabilityValue(SQLModel, table=True):
    """Admin-configurable list (C9). No inherent ranking -- exact-match caveat."""

    __tablename__ = "releasability_values"

    id: int | None = Field(default=None, primary_key=True)
    value: str = Field(unique=True, index=True)
    active: bool = Field(default=True)


class Document(SQLModel, table=True):
    """System of record for a document's status and metadata (Section 6.3).
    Chunk vectors + a copy of this payload live in Qdrant once FR-5/FR-6 are
    implemented; this row is what curation (Section 4.2) and audit act on."""

    __tablename__ = "documents"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    filename: str
    uploader_sub: str
    uploader_username: str
    owner_org: str

    classification: str
    # FR-20/Section 6.3: exactly one Releasability value per document (no
    # multi-select, no chunk-level override) -- not a list, unlike
    # access_scope below, which is explicitly "one or more" per Section 6.3.
    releasability: str
    access_scope: list[str] = Field(sa_column=Column(JSON))  # orgs/groups/users or "PUBLIC"
    source_originator: str
    doc_type: str
    program_community: str | None = None
    effective_date: str | None = None

    status: str = Field(default="queued")
    # FR-8 progress states, in order: queued -> processing -> embedded ->
    # pending_review -> approved | rejected | superseded (FR-7 -- set when a
    # later submission naming this document as supersedes_document_id is
    # approved) | failed (parsing/embedding/storage error -- see processing_error)
    rejection_reason: str | None = None
    processing_error: str | None = None
    reviewed_by_sub: str | None = None
    reviewed_at: datetime | None = None
    chunk_count: int = Field(default=0)

    # FR-7: re-ingestion/versioning. Set at submission time (app/routes/upload.py)
    # when an uploader marks this as a new version of an existing approved
    # document; the swap (deleting the old document's Qdrant chunks, flipping
    # its status to `superseded`) happens atomically with curator approval of
    # *this* row, not at submission time -- see app/routes/curate.py.
    supersedes_document_id: uuid.UUID | None = Field(default=None, foreign_key="documents.id")

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AuditLogEntry(SQLModel, table=True):
    """Every ingestion, curation, and retrieval-relevant event (FR-31), keyed on
    the actor's OIDC identity rather than a self-reported name."""

    __tablename__ = "audit_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    actor_sub: str
    actor_username: str
    action: str  # e.g. "document.submit", "document.approve", "document.reject", "query"
    target_id: str | None = None
    detail: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)


class OAuthState(SQLModel, table=True):
    """Short-lived, one-time-use row backing the ingestion UI's OIDC
    Authorization Code + PKCE login (ARCHITECTURE.md Section 4.4) -- id is the
    `state` value handed to Keycloak and echoed back at /auth/callback, bound
    to the same browser via a cookie holding the identical value. Deleted
    once consumed; an abandoned row is harmless (never redeemable without the
    matching cookie) and low-volume enough not to need a cleanup job here."""

    __tablename__ = "oauth_states"

    id: str = Field(primary_key=True)
    code_verifier: str
    created_at: datetime = Field(default_factory=_utcnow)


class UserSession(SQLModel, table=True):
    """Server-side session backing the ingestion UI's browser login (Section
    4.4). The cookie only carries this row's id (an opaque token), never the
    access/refresh token itself, so a session is individually revocable
    (delete the row) and the tokens never touch JS-reachable storage."""

    __tablename__ = "user_sessions"

    id: str = Field(primary_key=True)
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=_utcnow)


class Notification(SQLModel, table=True):
    """FR-15: the uploader is notified of a curator's decision. No SMTP/email
    infra in this dev stack -- this is a discrete, markable-as-read record
    (app/routes/notifications.py) rather than email/push, but distinct from
    just checking GET /documents/{id} directly, which requires already
    knowing which document to check."""

    __tablename__ = "notifications"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    recipient_sub: str
    document_id: uuid.UUID = Field(foreign_key="documents.id")
    decision: str  # approved | rejected
    message: str
    read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
