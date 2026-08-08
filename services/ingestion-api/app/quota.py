"""Per-identity upload admission control (NFR-17 residual, issue #424).

NFR-17 puts request-rate limiting at the ingress layer on purpose (#209), and
`MAX_UPLOAD_BYTES` bounds a single request. Neither bounds *how much* one identity
submits in total, and rate limiting structurally cannot: an identity submitting one
individually-compliant 50MB file every few seconds stays under any sane
requests-per-second limit while still filling a finite air-gapped object store and
growing the curator queue without limit. That is admission control, not rate
limiting, and only the application knows what "one identity's documents" means.

Two independent caps, because they bound different resources:

- **In-flight documents** (`queued`/`processing`/`embedded`/`pending_review`):
  bounds curator-queue growth and worker load. Self-healing -- curating or
  rejecting a document frees the capacity again, so a legitimate bulk uploader is
  throttled rather than permanently blocked.
- **Bytes over a rolling 24 hours**: bounds durable storage, which curation does
  *not* free (NFR-12 keeps the original, and purge is a separate deliberate act).

Neither implies the other. A count cap alone leaves storage unbounded (50MB files,
approved as fast as they arrive); a byte cap alone lets someone flood the review
queue with 1KB files.

Both are admin-configurable (`PortalSettings`), for the same reason the
classification vocabulary is: a bulk-ingest deployment and an occasional-submitter
one want different numbers. `0` means unlimited, for a deployment that enforces
this at the platform layer instead.

Deliberately *not* here: the byte total counts rows this identity submitted,
whatever their current status, including `rejected` and `approved`. Counting only
in-flight bytes would let a rejected-then-resubmitted loop consume storage without
ever moving the total, which is the exact abuse pattern the cap exists to stop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from common.claims import UserClaims
from common.models import AuditLogEntry, Document, PortalSettings

# Statuses that mean "this document is still consuming review/worker capacity".
# `failed` is excluded: it consumes no further attention and its bytes are still
# counted by the storage cap, so excluding it here does not create a loophole.
IN_FLIGHT_STATUSES = ("queued", "processing", "embedded", "pending_review")

# Defaults for a deployment that has not set a policy. Chosen to bound the abuse
# case without interfering with legitimate use: the dev stack seeds 7 documents,
# and 20 GiB/24h is roughly 400 files at the 50MB per-file ceiling. A deployment
# with a real policy sets its own numbers in Admin; the point of a non-zero
# default is that upgrading into this release closes the gap rather than leaving
# it open until someone remembers to configure it.
DEFAULT_MAX_INFLIGHT = 200
DEFAULT_MAX_BYTES_24H = 20 * 1024 * 1024 * 1024

ROLLING_WINDOW = timedelta(hours=24)


class QuotaLimits:
    """Resolved caps. 0 means unlimited."""

    __slots__ = ("max_bytes_24h", "max_inflight")

    def __init__(self, max_inflight: int, max_bytes_24h: int) -> None:
        self.max_inflight = max_inflight
        self.max_bytes_24h = max_bytes_24h

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"QuotaLimits(max_inflight={self.max_inflight}, max_bytes_24h={self.max_bytes_24h})"


def resolve_limits(session: Session) -> QuotaLimits:
    """The caps in force, from PortalSettings, falling back to the defaults.

    A missing settings row or a null column resolves to the *default*, not to
    unlimited: a deployment that upgrades into this release should gain the bound
    it did not previously have. Choosing unlimited is an explicit act (set 0).
    """
    settings = session.get(PortalSettings, 1)
    max_inflight = getattr(settings, "upload_quota_max_inflight", None) if settings else None
    max_bytes = getattr(settings, "upload_quota_max_bytes_24h", None) if settings else None
    return QuotaLimits(
        DEFAULT_MAX_INFLIGHT if max_inflight is None else max_inflight,
        DEFAULT_MAX_BYTES_24H if max_bytes is None else max_bytes,
    )


def count_in_flight(session: Session, uploader_sub: str) -> int:
    return int(
        session.exec(
            select(func.count())
            .select_from(Document)
            .where(Document.uploader_sub == uploader_sub)
            .where(Document.status.in_(IN_FLIGHT_STATUSES))  # type: ignore[attr-defined]
        ).one()
    )


def bytes_in_window(session: Session, uploader_sub: str, *, now: datetime | None = None) -> int:
    """Bytes this identity has submitted inside the rolling window.

    `content_bytes` is null on rows predating the column, and SUM ignores nulls,
    so those count as 0. That under-counts historical usage rather than locking
    someone out over data the system cannot see -- the safer direction for a
    control that returns 429.
    """
    since = (now or datetime.now(UTC)) - ROLLING_WINDOW
    total = session.exec(
        select(func.coalesce(func.sum(Document.content_bytes), 0))
        .where(Document.uploader_sub == uploader_sub)
        .where(Document.created_at >= since)
    ).one()
    return int(total or 0)


def enforce_upload_quota(
    session: Session,
    user: UserClaims,
    incoming_bytes: int,
    *,
    filename: str,
    now: datetime | None = None,
) -> None:
    """Raise 429 if accepting `incoming_bytes` would exceed this identity's quota.

    Called from `_ingest_one_file` after the bytes are read (the size has to be
    known) but **before** the object-store write and the Document row, so a
    refused upload consumes no durable capacity. Both upload paths share that
    function, which is what makes a batch count every file against the quota
    rather than counting as one request (#424's fourth point).

    A denial is audit-logged and committed before raising: the exception unwinds
    the request, so an uncommitted entry would be rolled back and the refusal
    would leave no trace. Every other write in this path has already been
    committed per-file by the time this runs, so the extra commit cannot publish
    a partial document.
    """
    limits = resolve_limits(session)

    if limits.max_inflight:
        in_flight = count_in_flight(session, user.sub)
        if in_flight + 1 > limits.max_inflight:
            _deny(
                session,
                user,
                filename=filename,
                reason="in_flight",
                detail={
                    "in_flight": in_flight,
                    "limit": limits.max_inflight,
                },
                message=(
                    f"upload quota reached: {in_flight} of your documents are already "
                    f"awaiting processing or review (limit {limits.max_inflight}). "
                    f"They free capacity as curators review them."
                ),
            )

    if limits.max_bytes_24h:
        used = bytes_in_window(session, user.sub, now=now)
        if used + incoming_bytes > limits.max_bytes_24h:
            _deny(
                session,
                user,
                filename=filename,
                reason="bytes_24h",
                detail={
                    "used_bytes": used,
                    "incoming_bytes": incoming_bytes,
                    "limit_bytes": limits.max_bytes_24h,
                },
                message=(
                    f"upload quota reached: {_human_bytes(used)} of "
                    f"{_human_bytes(limits.max_bytes_24h)} "
                    f"submitted in the last 24 hours. Capacity frees as that window rolls."
                ),
            )


def _human_bytes(value: int) -> str:
    """Byte count in a unit that carries information.

    Formatting everything as GiB made the message useless for any cap below a
    gigabyte: a 10,000-byte limit rendered as "0.00 GiB of 0.00 GiB submitted",
    which tells the submitter nothing and looks like a bug. Found by running the
    refusal against a real Postgres rather than by reading the code.
    """
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= size:
            return f"{value / size:.2f} {unit}"
    return f"{value} bytes"


def _deny(
    session: Session,
    user: UserClaims,
    *,
    filename: str,
    reason: str,
    detail: dict[str, int],
    message: str,
) -> None:
    session.add(
        AuditLogEntry(
            actor_sub=user.sub,
            actor_username=user.preferred_username,
            action="document.submit.quota_denied",
            target_id=None,
            detail={"filename": filename, "reason": reason, **detail},
        )
    )
    session.commit()
    raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, message)
