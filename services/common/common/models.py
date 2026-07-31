"""Postgres tables: system of record for document status, audit log, and the
admin-configurable Classification/Releasability lists (C9). Qdrant remains the
vector store; these tables are the transactional source of truth (see plan notes
in REQUIREMENTS.md Section 6.3 and C9)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from common.token_crypto import EncryptedString


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


class PortalSettings(SQLModel, table=True):
    """Issue #166: deployment-wide portal settings -- the classification
    banner, and the visual theme.

    Admin-set, not derived. The banner states what this *system* is accredited
    to hold, which is a deployment property an accrediting authority decides --
    not something to infer from whoever happens to be signed in. Deriving it
    from a user's clearance would make the same page carry different markings
    for different viewers, which is exactly what a marking must not do.

    A single row (id=1). Absent or inactive means no banner has been set, and
    the portal says so explicitly rather than defaulting to UNCLASSIFIED --
    "nobody has configured this" and "this system holds unclassified material"
    are different statements, and quietly showing the second when the first is
    true is how a wrong marking ends up on a screen.
    """

    __tablename__ = "portal_settings"

    id: int | None = Field(default=None, primary_key=True)
    # Free text rather than a foreign key to classification_levels: a banner
    # is a full marking line ("SECRET//NOFORN"), not a single level, and the
    # accreditation may word it in ways the ranked list does not contain.
    text: str = Field(default="")
    # Drives the banner colour. Matched case-insensitively against the CAPCO
    # palette in portal.css; anything unrecognised falls back to a neutral
    # style rather than guessing a colour, since a wrong colour on a marking
    # is worse than no colour.
    level: str = Field(default="")
    active: bool = Field(default=False)
    # Selected in Admin > Portal settings, applied as a data-theme attribute
    # the stylesheet keys its token block off. Deployment-wide rather than
    # per-user: two people looking at the same classified record should see
    # the same page, and a per-user theme is a small step towards them not
    # doing so. Empty means the built-in default.
    theme: str = Field(default="")

    # Issue #248: branding, shown on the login landing page (#246) and
    # persistently in the site header/tab title everywhere else. Empty means
    # the built-in "Document Portal" default and no logo -- base.html falls
    # back rather than rendering an empty <img>.
    app_name: str = Field(default="")
    logo_url: str = Field(default="")

    # Issue #246/#248: the login page's call-to-action button. Empty means
    # the DEFAULT_LOGIN_BUTTON_TEXT constant in app/main.py.
    login_button_text: str = Field(default="")

    # Issue #248: mandatory-acceptance warning banner shown as a popup on the
    # login landing page (distinct from the CAPCO classification banner
    # above). Mirrors text/active's "no admin has set a marking" reasoning --
    # empty text can't be active, since a popup with nothing in it would just
    # be a broken dialog rather than the absence of one.
    login_popup_title: str = Field(default="")
    login_popup_text: str = Field(default="")
    login_popup_active: bool = Field(default=False)

    updated_by: str | None = Field(default=None)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
    # FR-20/Section 6.3: one or more Releasability values per document (no
    # chunk-level override) -- e.g. ["NATO", "FVEY"] renders as "REL TO NATO,
    # FVEY". A document is visible to a querying user if any one of these
    # values is either NO_RELEASABILITY_RESTRICTION or held by that user (see
    # qdrant_filters.build_access_filter) -- same "any element in common"
    # semantics as access_scope below, just a different vocabulary.
    releasability: list[str] = Field(sa_column=Column(JSON))
    # orgs/groups/users, or "ALL_AUTHENTICATED"
    access_scope: list[str] = Field(sa_column=Column(JSON))
    source_originator: str
    doc_type: str
    program_community: str | None = None
    effective_date: str | None = None

    # NFR-12: where the original uploaded file lives in the object store
    # (common/object_store.py), independent of this row and of Qdrant's
    # chunk vectors. Set at submission time, before processing ever starts --
    # see app/routes/upload.py.
    original_object_key: str | None = None

    # Issue #138 (Phase 1): advisory, curator-facing marking-mismatch findings
    # computed by the ingestion worker (common/marking_detection.py) -- e.g.
    # "the document's own banner says SECRET but it was tagged CUI". Purely
    # advisory: it is NOT one of the authoritative human-set tags above and is
    # never used to gate retrieval or auto-change classification; the curator
    # confirms or overrides it (FR-13). Null until the worker evaluates it, and
    # left null if detection errors -- ingestion never depends on it.
    tagging_advisory: dict | None = Field(default=None, sa_column=Column(JSON))

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

    # The submission row and original are committed before JetStream publish.
    # This timestamp is the durable hand-off marker: null means the API's
    # reconciliation loop must retry publishing this queued document. It is
    # only set after JetStream acknowledges persistence, so a process crash or
    # broker outage at the Postgres -> NATS boundary cannot strand the row.
    queue_published_at: datetime | None = None

    # A short processing lease makes duplicate publication safe without
    # defeating crash recovery. A duplicate delivered while another worker is
    # actively processing is acknowledged as redundant; once this timestamp is
    # older than the JetStream ack wait, a redelivery may reclaim the job.
    processing_started_at: datetime | None = None

    # FR-7: re-ingestion/versioning. Set at submission time (app/routes/upload.py)
    # when an uploader marks this as a new version of an existing approved
    # document; the swap (deleting the old document's Qdrant chunks, flipping
    # its status to `superseded`) happens atomically with curator approval of
    # *this* row, not at submission time -- see app/routes/curate.py.
    supersedes_document_id: uuid.UUID | None = Field(default=None, foreign_key="documents.id")

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PurgeRequest(SQLModel, table=True):
    """Issue #279 (gap G3): the two-person half of destruction. Recording
    this row destroys nothing by itself -- `common/purge.py`'s
    `purge_document` only ever runs once a *different* `rag-purge` holder
    confirms (`confirming_sub != requested_by_sub`, enforced by
    `purge_confirmation_authorized`). `status` only ever moves
    pending -> confirmed; there is no background sweep for the expiry window
    -- `confirm_purge` simply refuses a request once `expires_at` has
    passed, which is enough to keep a stale request from ever being acted on
    without needing a scheduled job."""

    __tablename__ = "purge_requests"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    document_id: uuid.UUID = Field(foreign_key="documents.id")
    reason: str
    requested_by_sub: str
    requested_by_username: str
    requested_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    status: str = Field(default="pending")  # pending | confirmed
    confirmed_by_sub: str | None = None
    confirmed_by_username: str | None = None
    confirmed_at: datetime | None = None


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
    to the same browser via a cookie holding the identical value.

    Deleted once consumed, and `created_at` is enforced as a TTL
    (auth.OAUTH_STATE_TTL) at /callback -- an abandoned row stops being
    redeemable rather than staying valid indefinitely. Issue #108: the
    claim that abandoned rows were "low-volume enough not to need a cleanup
    job" didn't hold, since /auth/login is unauthenticated and wrote one row
    per call with nothing reaping them; auth._purge_expired now does."""

    __tablename__ = "oauth_states"

    id: str = Field(primary_key=True)
    code_verifier: str
    created_at: datetime = Field(default_factory=_utcnow)


class UserSession(SQLModel, table=True):
    """Server-side session backing the ingestion UI's browser login (Section
    4.4). The cookie only carries this row's id (an opaque token), never the
    access/refresh token itself, so a session is individually revocable
    (delete the row) and the tokens never touch JS-reachable storage.

    Issue #213: access_token/refresh_token/id_token are encrypted at rest
    (EncryptedString, common/token_crypto.py) -- a read-only compromise of
    the app database alone must not yield live, usable Keycloak credentials.
    Application code (app/deps.py, app/routes/auth.py) is unaffected: the
    TypeDecorator encrypts/decrypts transparently at the ORM boundary, so
    these are still read and written as plain `str`."""

    __tablename__ = "user_sessions"

    id: str = Field(primary_key=True)
    access_token: str = Field(sa_column=Column(EncryptedString, nullable=False))
    # Issue #108: `created_at` below is load-bearing, not just informational
    # -- deps.session_expired() enforces an absolute SESSION_LIFETIME from
    # it. Without that, _refresh_session would renew this row for as long as
    # Keycloak honoured the refresh token, so a captured session id outlived
    # the lifetime the cookie's max_age implied.
    refresh_token: str | None = Field(default=None, sa_column=Column(EncryptedString))
    # Kept only for Keycloak RP-initiated logout's `id_token_hint` param
    # (app/routes/auth.py's logout()) -- never used for claims/auth checks,
    # which stay on access_token via the same parse_claims() as the
    # header-auth path.
    id_token: str | None = Field(default=None, sa_column=Column(EncryptedString))
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
