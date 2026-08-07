"""Issue #432 (NFR-18 follow-on): periodic, event-independent re-verification
of object-store originals against their stored `content_sha256` digest.

#285 shipped the event-triggered half -- ingestion-worker re-hashes an
original every time it's fetched for parsing (`processing.py`) or
re-embedding (`reembed.py`), refusing to proceed on mismatch. Neither of
those ever runs for the common case: an approved document whose original
just sits in the object store, untouched, for months. NFR-18 names the gap
directly: "store-side tampering, backup restore, bit rot" go undetected for
that entire population until nothing checks it. This module is that check.

Deliberately does NOT refuse/fail/re-process anything on a mismatch, unlike
the two event-triggered call sites -- there is no processing in flight here
to refuse, and the cause (bit rot vs. real tampering) needs human triage, not
an automatic status change (issue #432 suggested-direction item 2). A finding
is recorded as an `audit_log` entry (`document.integrity_check_failed`) and
counted toward the `nexus_rag_integrity_check_failures_total` Pushgateway
metric so it surfaces the same way `NexusRagQueryAnomalyDetected` does
(`scripts/detect_query_anomalies.py`) -- an operator/curator decides what
to do next.

Bounded by a rolling window rather than a full-corpus pass every run (issue
#432 suggested-direction item 3): each invocation checks at most
`--batch-size` documents, oldest-`last_verified_at`-first (nulls, i.e. never
yet checked, sort first). A corpus larger than one run's batch size is
covered incrementally across repeated runs (the CronJob's schedule) rather
than paying a full re-hash's I/O cost in one shot.

Deliberately an offline, scheduled mode (`python -m app.integrity_sweep`),
the same posture `reembed.py`'s module docstring describes for its own CLI:
separate from the request-serving paths, run by an operator or a CronJob,
never triggered by an upload or a query.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlmodel import Session, select

from common.db import get_engine, init_db
from common.log_safety import log_safe
from common.models import AuditLogEntry, Document
from common.object_store import get_object_store

logger = logging.getLogger("ingestion-worker.integrity_sweep")

DEFAULT_BATCH_SIZE = 500
DEFAULT_GATEWAY = os.environ.get("RAG_INTEGRITY_PUSHGATEWAY_URL", "http://127.0.0.1:9092")


@dataclass
class SweepReport:
    """What one sweep run actually did -- returned rather than only logged so
    the CLI and tests can assert on it without scraping log output."""

    checked: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    mismatched: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)

    @property
    def failures(self) -> int:
        return len(self.mismatched) + len(self.missing)


def _candidates(session: Session, batch_size: int) -> list[uuid.UUID]:
    """The rolling window: rows with a digest to check against and an
    original that (per this row) still exists, oldest-checked first. Purged
    rows (`original_object_key is None`) and pre-#285 rows (`content_sha256
    is None`) have nothing this sweep can verify, so they're excluded at the
    query rather than surfacing as a spurious "missing" finding."""
    query = (
        select(Document.id)
        .where(Document.content_sha256.is_not(None))  # type: ignore[union-attr]
        .where(Document.original_object_key.is_not(None))  # type: ignore[union-attr]
        .order_by(Document.last_verified_at.asc().nulls_first())  # type: ignore[union-attr]
        .limit(batch_size)
    )
    return list(session.exec(query).all())


def _verify_one(session: Session, document_id: uuid.UUID, report: SweepReport) -> None:
    doc = session.get(Document, document_id)
    if doc is None or doc.content_sha256 is None or doc.original_object_key is None:
        return  # raced with a purge/edit between _candidates and here

    label = str(document_id)
    report.checked.append(label)

    try:
        contents = get_object_store().get(doc.original_object_key)
    except Exception as exc:
        # backend-specific (FileNotFoundError, boto3 ClientError/NoSuchKey,
        # ...); any of them means "the original this row expects is not
        # retrievable", which is itself a finding.
        logger.error("original unreadable for %s: %s", log_safe(label), log_safe(exc))
        report.missing[label] = f"{type(exc).__name__}: {exc}"
        session.add(
            AuditLogEntry(
                actor_sub="system",
                actor_username="integrity-sweep",
                action="document.integrity_check_failed",
                target_id=label,
                detail={"reason": "original_unreadable", "error_type": type(exc).__name__},
            )
        )
        session.commit()
        return

    actual = hashlib.sha256(contents).hexdigest()
    now = datetime.now(UTC)
    if actual != doc.content_sha256:
        logger.error("content mismatch for %s", log_safe(label))
        report.mismatched[label] = f"expected sha256 {doc.content_sha256}, got {actual}"
        # Deliberately no digests in `detail` -- purge.py's own comment on
        # this same tension: a content digest is itself a content
        # fingerprint, so it's excluded here the same way it's scrubbed from
        # a purge tombstone. `last_verified_at` is also deliberately left
        # unset on a mismatch (see Document.last_verified_at's comment) --
        # the row stays at the front of the next run's rolling window until
        # a human resolves it, instead of quietly aging out of view.
        session.add(
            AuditLogEntry(
                actor_sub="system",
                actor_username="integrity-sweep",
                action="document.integrity_check_failed",
                target_id=label,
                detail={"reason": "digest_mismatch"},
            )
        )
        session.commit()
        return

    report.verified.append(label)
    doc.last_verified_at = now
    session.add(doc)
    session.commit()


def run_sweep(*, batch_size: int = DEFAULT_BATCH_SIZE) -> SweepReport:
    report = SweepReport()
    with Session(get_engine()) as session:
        candidates = _candidates(session, batch_size)
        for document_id in candidates:
            _verify_one(session, document_id, report)
    return report


# ---------------------------------------------------------------------------
# Prometheus/Pushgateway export -- same exposition-text/PUT convention as
# `scripts/detect_query_anomalies.py` and `publish_rag_quality_metrics.py`.

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _metric_line(name: str, value: int | float) -> str:
    return f"{name} {value:.17g}" if isinstance(value, float) else f"{name} {value}"


def _family(name: str, help_text: str, lines: list[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *lines]


def build_exposition(report: SweepReport, run_timestamp: float) -> str:
    lines: list[str] = []
    lines.extend(
        _family(
            "nexus_rag_integrity_check_failures_total",
            "Count of documents whose object-store original failed the most "
            "recent periodic re-verification against content_sha256 (#432) -- "
            "digest mismatch or unreadable original, either resolved by "
            "operator/curator triage, not automatically.",
            [_metric_line("nexus_rag_integrity_check_failures_total", report.failures)],
        )
    )
    lines.extend(
        _family(
            "nexus_rag_integrity_check_documents_checked",
            "Number of documents the most recent sweep run actually checked "
            "(its rolling-window batch size, or fewer if the corpus is smaller).",
            [_metric_line("nexus_rag_integrity_check_documents_checked", len(report.checked))],
        )
    )
    lines.extend(
        _family(
            "nexus_rag_integrity_check_last_run_timestamp_seconds",
            "Unix timestamp of the most recent integrity sweep run -- alert on "
            "staleness if this job stops running, since a Pushgateway value "
            "otherwise persists silently after the job that set it goes away.",
            [_metric_line("nexus_rag_integrity_check_last_run_timestamp_seconds", run_timestamp)],
        )
    )
    return "\n".join(lines) + "\n"


def publish(gateway_url: str, payload: str, *, timeout: float = 10.0) -> None:
    root = gateway_url.rstrip("/")
    url = f"{root}/metrics/job/nexus-rag-integrity-sweep"
    with httpx.Client(timeout=timeout) as client:
        response = client.put(url, content=payload, headers={"Content-Type": _CONTENT_TYPE})
        response.raise_for_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Periodically re-verify object-store originals against their stored "
        "content_sha256 digest, independent of any upload/re-embed event (issue #432). "
        "Run inside the ingestion-worker container/image, e.g. "
        "`docker compose run --rm ingestion-worker python -m app.integrity_sweep`."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="max documents to check in this run -- the rolling window (default: %(default)s)",
    )
    parser.add_argument("--pushgateway-url", default=DEFAULT_GATEWAY)
    parser.add_argument(
        "--no-push", action="store_true", help="skip the Pushgateway export, report only"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()

    report = run_sweep(batch_size=args.batch_size)

    print(f"checked: {len(report.checked)}")
    print(f"verified: {len(report.verified)}")
    if report.mismatched:
        print(f"digest mismatch: {len(report.mismatched)}")
        for label, reason in report.mismatched.items():
            print(f"  {label}: {reason}")
    if report.missing:
        print(f"unreadable original: {len(report.missing)}")
        for label, reason in report.missing.items():
            print(f"  {label}: {reason}")

    if not args.no_push:
        payload = build_exposition(report, datetime.now(UTC).timestamp())
        try:
            publish(args.pushgateway_url, payload)
        except Exception as exc:  # fail-open: reporting must not crash on a Pushgateway outage
            print(f"WARNING: could not publish to Pushgateway: {exc}", file=sys.stderr)

    # Exit 0 even when findings exist -- like detect_query_anomalies.py, a
    # flagged document is a successful detection, not a job failure. Only an
    # exception escaping run_sweep() (DB unreachable, etc.) is a job failure,
    # and that propagates and exits non-zero on its own.
    return 0


if __name__ == "__main__":
    sys.exit(main())
