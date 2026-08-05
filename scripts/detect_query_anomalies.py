#!/usr/bin/env python3
"""Issue #426 (#127 gap #4 / threat-model residual): detect reconnaissance-
shaped retrieval patterns from the FR-31 audit log -- one identity issuing
many near-identical or systematically narrow queries, or immediately using
a `rag-query` grant that just changed state. #127 named the core threat
explicitly: an authorized `rag-query` user can probe with crafted queries
and use the result set (including an *absent* result) to infer whether a
specific document exists in the corpus. The audit log has carried every
field this needs since #72/#73 shipped; nothing read it for this purpose
until now.

Why this connects to Postgres directly, as its own SELECT-only role, the same
way `calibrate_tagging_advisory.py` (#309) does: NFR-2 makes every
application role's own database credentials INSERT-only on `audit_log` (see
`infra/postgres/apply-service-grants.sh`), so reading it is always a
distinct, attributable, offline act -- never something a compromised service
could already do. This script authenticates as `nexus_rag_audit_reporting`
(the same role, no new grant needed) and, like the calibration script, is run
manually or on a schedule, never wired into any service's request path.
`docs/governance.md`'s "no API route reads the audit log" stays true: this is
an offline job, not a route.

Signals computed per `actor_sub` over a lookback window, from `query` and
`query.denied` rows only (`query.failed` -- the #122 embedding-mismatch guard
-- is an operational condition, not a user action, and is excluded):

- **high_volume**: total attempts (`query` + `query.denied`) at or above
  `--rate-threshold`. Catches naive scripted probing -- suggested direction
  item 2 of #426.
- **high_denial_ratio**: `query.denied` share of attempts at or above
  `--denial-ratio-threshold`, gated by a minimum attempt count so a single
  denial for a brand-new user isn't 100% of one. `NexusRagQueryDeniedSpike`
  (`infra/observability/prometheus/rules/nexus-rag.yml`) already catches raw
  denial *volume* across all identities; this catches one identity's denial
  *rate*, which volume alone would miss if it stayed under the aggregate
  threshold.
- **narrow_probe_shaped**: share of *successful* queries returning 0 or 1
  chunks at or above `--singleton-threshold`, gated by a minimum success
  count. This is the substitute for #426's literal "near-duplicate query
  text" suggestion (item 3): #125 deliberately never stores query text
  (`orchestration-mcp/app/rag_search.py::_audit_query_detail`), so there is
  no text to diff. A user running many queries that each resolve to at most
  one chunk is the same membership-inference shape OWASP describes --
  crafted, narrow questions checking one document at a time -- and
  `result_count` already carries that signal without needing content.
- **boundary_mapping**: count of `query.denied` -> `query` (success)
  transitions for the same actor within `--sequence-window-seconds` of each
  other, at or above `--sequence-threshold`. Narrower than #426's suggested
  "filter-boundary mapping" framing turned out to be live: `rag_search.py`'s
  only `query.denied` path is the coarse missing-`rag-query`-role gate
  (`if not claims.can_query`), not a per-query FR-26 classification/
  releasability/access-scope mismatch -- an out-of-scope query returns a
  *successful* empty result, never a denial (confirmed against a live
  stack: sending an unscoped identity 11 queries produced 11 `query.denied`
  rows and zero `query` rows, so there was nothing for a denial-then-success
  transition to match). What this signal actually detects is an identity's
  `rag-query` grant changing state mid-window and being used immediately
  after -- e.g. a role revoked then reinstated, or a delayed token refresh
  picking up a just-granted role -- which is still worth a human look, just
  a different (narrower, rarer) shape than probing where an access filter's
  edge sits.

Deliberately NOT a per-identity Prometheus label. `orchestration-mcp/app/
metrics.py`'s module docstring already rejects that design for this exact
reason: "a per-user label would rebuild exactly the surveillance surface
#125 removed from the audit log," and unbounded identity cardinality is its
own operational risk (#426 item 4). What reaches Prometheus (via Pushgateway,
`publish_rag_quality_metrics.py`'s pattern) is a *count of flagged
identities* per signal plus the worst-case raw value -- enough for a
content-free, bounded alert. The actual `actor_sub`/`actor_username` values
only appear in this script's own stdout report, read by whoever holds the
`nexus_rag_audit_reporting` credential -- the same audience `docs/
governance.md`'s audit-reporting table already names.

Reporting only, no pass/fail gate by default, same posture as
`calibrate_tagging_advisory.py` (`docs/testing.md`).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

DEFAULT_HOST = os.environ.get("POSTGRES_HOST", "postgres")
DEFAULT_PORT = os.environ.get("POSTGRES_PORT", "5432")
DEFAULT_DB = os.environ.get("POSTGRES_DB", "nexus_rag")
DEFAULT_USER = os.environ.get("AUDIT_REPORTING_DB_USER", "nexus_rag_audit_reporting")
DEFAULT_PASSWORD = os.environ.get("AUDIT_REPORTING_DB_PASSWORD", "nexus_rag_audit_reporting")

_QUERY_ACTIONS = ("query", "query.denied")

DEFAULT_LOOKBACK_MINUTES = 60
DEFAULT_RATE_THRESHOLD = 30
DEFAULT_DENIAL_RATIO_THRESHOLD = 0.3
DEFAULT_MIN_ATTEMPTS_FOR_RATIO = 10
DEFAULT_SINGLETON_THRESHOLD = 0.6
DEFAULT_MIN_SUCCESS_FOR_SINGLETON = 10
DEFAULT_SEQUENCE_WINDOW_SECONDS = 300
DEFAULT_SEQUENCE_THRESHOLD = 5


def default_dsn() -> str:
    override = os.environ.get("AUDIT_REPORTING_DATABASE_URL")
    if override:
        return override
    return (
        f"postgresql://{DEFAULT_USER}:{DEFAULT_PASSWORD}@{DEFAULT_HOST}:{DEFAULT_PORT}/{DEFAULT_DB}"
    )


def fetch_query_events(dsn: str, since: datetime) -> list[dict]:
    """Every `query`/`query.denied` audit row at or after `since`, oldest
    first. `query.failed` (embedding-mismatch guard) is excluded at the SQL
    level -- it is an operational condition, never a user action.

    `psycopg` is imported here, not at module level, for the same reason
    `calibrate_tagging_advisory.py::fetch_decisions` does: it is a
    scripts/-only dependency the repo-root `unit` CI job never installs, and
    the tests below only exercise the pure `aggregate()` logic.
    """
    import psycopg

    query = (
        "SELECT actor_sub, actor_username, action, detail, created_at "
        "FROM audit_log WHERE action = ANY(%s) AND created_at >= %s ORDER BY created_at"
    )
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(query, [list(_QUERY_ACTIONS), since])
        rows = cur.fetchall()
    return [
        {
            "actor_sub": r[0],
            "actor_username": r[1],
            "action": r[2],
            "detail": r[3] or {},
            "created_at": r[4],
        }
        for r in rows
    ]


@dataclass
class IdentityStats:
    actor_sub: str
    actor_username: str
    total_attempts: int = 0
    denied_count: int = 0
    success_count: int = 0
    singleton_or_empty_count: int = 0
    boundary_mapping_count: int = 0
    signals: list[str] = field(default_factory=list)

    @property
    def denial_ratio(self) -> float:
        return self.denied_count / self.total_attempts if self.total_attempts else 0.0

    @property
    def singleton_or_empty_rate(self) -> float:
        return self.singleton_or_empty_count / self.success_count if self.success_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_sub": self.actor_sub,
            "actor_username": self.actor_username,
            "total_attempts": self.total_attempts,
            "denied_count": self.denied_count,
            "denial_ratio": round(self.denial_ratio, 4),
            "success_count": self.success_count,
            "singleton_or_empty_rate": round(self.singleton_or_empty_rate, 4),
            "boundary_mapping_count": self.boundary_mapping_count,
            "signals": self.signals,
        }


@dataclass
class AnomalyReport:
    window_seconds: int
    identities: dict[str, IdentityStats] = field(default_factory=dict)

    def flagged(self) -> list[IdentityStats]:
        return [stats for stats in self.identities.values() if stats.signals]

    def max_attempts(self) -> int:
        return max((s.total_attempts for s in self.identities.values()), default=0)

    def flagged_counts(self) -> dict[str, int]:
        counts = {
            "high_volume": 0,
            "high_denial_ratio": 0,
            "narrow_probe_shaped": 0,
            "boundary_mapping": 0,
        }
        for stats in self.flagged():
            for signal in stats.signals:
                counts[signal] += 1
        return counts


def aggregate(
    events: list[dict],
    *,
    window_seconds: int,
    rate_threshold: int = DEFAULT_RATE_THRESHOLD,
    denial_ratio_threshold: float = DEFAULT_DENIAL_RATIO_THRESHOLD,
    min_attempts_for_ratio: int = DEFAULT_MIN_ATTEMPTS_FOR_RATIO,
    singleton_threshold: float = DEFAULT_SINGLETON_THRESHOLD,
    min_success_for_singleton: int = DEFAULT_MIN_SUCCESS_FOR_SINGLETON,
    sequence_window_seconds: int = DEFAULT_SEQUENCE_WINDOW_SECONDS,
    sequence_threshold: int = DEFAULT_SEQUENCE_THRESHOLD,
) -> AnomalyReport:
    """Pure aggregation over already-fetched audit rows -- no DB access, so
    this is what the tests exercise directly, the same split
    `calibrate_tagging_advisory.py` uses between `fetch_decisions` and
    `aggregate`.

    `events` must already be ordered oldest-first (as `fetch_query_events`
    returns them) -- boundary-mapping detection is a chronological scan.
    """
    report = AnomalyReport(window_seconds=window_seconds)
    last_denial_at: dict[str, datetime] = {}

    for event in events:
        sub = event["actor_sub"]
        stats = report.identities.setdefault(
            sub, IdentityStats(actor_sub=sub, actor_username=event["actor_username"])
        )
        stats.total_attempts += 1
        action = event["action"]
        created_at = event["created_at"]

        if action == "query.denied":
            stats.denied_count += 1
            if created_at is not None:
                last_denial_at[sub] = created_at
            continue

        # action == "query": a successful attempt, whether or not it
        # returned candidates (result_count is present regardless).
        stats.success_count += 1
        result_count = event["detail"].get("result_count")
        if isinstance(result_count, int) and result_count <= 1:
            stats.singleton_or_empty_count += 1

        pending_denial = last_denial_at.get(sub)
        if (
            pending_denial is not None
            and created_at is not None
            and (created_at - pending_denial).total_seconds() <= sequence_window_seconds
        ):
            stats.boundary_mapping_count += 1
            del last_denial_at[sub]

    for stats in report.identities.values():
        if stats.total_attempts >= rate_threshold:
            stats.signals.append("high_volume")
        if (
            stats.total_attempts >= min_attempts_for_ratio
            and stats.denial_ratio >= denial_ratio_threshold
        ):
            stats.signals.append("high_denial_ratio")
        if (
            stats.success_count >= min_success_for_singleton
            and stats.singleton_or_empty_rate >= singleton_threshold
        ):
            stats.signals.append("narrow_probe_shaped")
        if stats.boundary_mapping_count >= sequence_threshold:
            stats.signals.append("boundary_mapping")

    return report


def print_report(report: AnomalyReport) -> None:
    flagged = sorted(report.flagged(), key=lambda s: s.total_attempts, reverse=True)
    print(f"Window: last {report.window_seconds}s, {len(report.identities)} identities observed")
    if not flagged:
        print("No reconnaissance-shaped patterns flagged.")
        return
    print(f"{len(flagged)} identities flagged:\n")
    for stats in flagged:
        print(f"  {stats.actor_username} ({stats.actor_sub})")
        print(f"    signals: {', '.join(stats.signals)}")
        print(
            f"    attempts={stats.total_attempts} denied={stats.denied_count} "
            f"({stats.denial_ratio:.0%}) success={stats.success_count} "
            f"singleton_or_empty_rate={stats.singleton_or_empty_rate:.0%} "
            f"boundary_mapping={stats.boundary_mapping_count}"
        )


# ---------------------------------------------------------------------------
# Prometheus/Pushgateway export -- content-free by construction: no
# actor_sub, actor_username, or query content, only counts. Same exposition
# format/PUT convention as `publish_rag_quality_metrics.py`.

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _metric_line(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    suffix = ""
    if labels:
        joined = ",".join(f'{key}="{val}"' for key, val in sorted(labels.items()))
        suffix = f"{{{joined}}}"
    return f"{name}{suffix} {value:.17g}" if isinstance(value, float) else f"{name}{suffix} {value}"


def _family(name: str, help_text: str, lines: list[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *lines]


def build_exposition(report: AnomalyReport, run_timestamp: float) -> str:
    lines: list[str] = []
    counts = report.flagged_counts()
    flagged_lines = [
        _metric_line("nexus_rag_query_anomaly_flagged_identities", count, {"signal": signal})
        for signal, count in sorted(counts.items())
    ]
    lines.extend(
        _family(
            "nexus_rag_query_anomaly_flagged_identities",
            "Count of distinct identities flagged for a reconnaissance-shaped query "
            "pattern signal in the most recent detection run (#426). No identity in "
            "this label -- see the run's stdout report for who.",
            flagged_lines,
        )
    )
    lines.extend(
        _family(
            "nexus_rag_query_anomaly_max_attempts",
            "Highest total query+query.denied attempt count observed for a single "
            "identity in the most recent detection window.",
            [_metric_line("nexus_rag_query_anomaly_max_attempts", report.max_attempts())],
        )
    )
    lines.extend(
        _family(
            "nexus_rag_query_anomaly_window_seconds",
            "Lookback window, in seconds, used by the most recent detection run.",
            [_metric_line("nexus_rag_query_anomaly_window_seconds", report.window_seconds)],
        )
    )
    lines.extend(
        _family(
            "nexus_rag_query_anomaly_last_run_timestamp_seconds",
            "Unix timestamp of the most recent detection run -- alert on staleness "
            "if this job stops running, since a Pushgateway value otherwise persists "
            "silently after the job that set it goes away.",
            [_metric_line("nexus_rag_query_anomaly_last_run_timestamp_seconds", run_timestamp)],
        )
    )
    return "\n".join(lines) + "\n"


def publish(gateway_url: str, payload: str, *, timeout: float = 10.0) -> None:
    root = gateway_url.rstrip("/")
    url = f"{root}/metrics/job/nexus-rag-query-anomaly-detection"
    with httpx.Client(timeout=timeout) as client:
        response = client.put(url, content=payload, headers={"Content-Type": _CONTENT_TYPE})
        response.raise_for_status()


DEFAULT_GATEWAY = os.environ.get("RAG_ANOMALY_PUSHGATEWAY_URL", "http://127.0.0.1:9092")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", type=str, default=None, help="override the Postgres DSN")
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=DEFAULT_LOOKBACK_MINUTES,
        help="how far back to scan the audit log (default: %(default)s)",
    )
    parser.add_argument("--rate-threshold", type=int, default=DEFAULT_RATE_THRESHOLD)
    parser.add_argument(
        "--denial-ratio-threshold", type=float, default=DEFAULT_DENIAL_RATIO_THRESHOLD
    )
    parser.add_argument(
        "--min-attempts-for-ratio", type=int, default=DEFAULT_MIN_ATTEMPTS_FOR_RATIO
    )
    parser.add_argument("--singleton-threshold", type=float, default=DEFAULT_SINGLETON_THRESHOLD)
    parser.add_argument(
        "--min-success-for-singleton", type=int, default=DEFAULT_MIN_SUCCESS_FOR_SINGLETON
    )
    parser.add_argument(
        "--sequence-window-seconds", type=int, default=DEFAULT_SEQUENCE_WINDOW_SECONDS
    )
    parser.add_argument("--sequence-threshold", type=int, default=DEFAULT_SEQUENCE_THRESHOLD)
    parser.add_argument("--pushgateway-url", default=DEFAULT_GATEWAY)
    parser.add_argument(
        "--no-push", action="store_true", help="skip the Pushgateway export, report only"
    )
    args = parser.parse_args()

    import psycopg

    dsn = args.dsn or default_dsn()
    since = datetime.now(UTC) - timedelta(minutes=args.lookback_minutes)

    try:
        events = fetch_query_events(dsn, since)
    except psycopg.OperationalError as exc:
        print(f"FAILED: could not connect to Postgres: {exc}", file=sys.stderr)
        return 1

    report = aggregate(
        events,
        window_seconds=args.lookback_minutes * 60,
        rate_threshold=args.rate_threshold,
        denial_ratio_threshold=args.denial_ratio_threshold,
        min_attempts_for_ratio=args.min_attempts_for_ratio,
        singleton_threshold=args.singleton_threshold,
        min_success_for_singleton=args.min_success_for_singleton,
        sequence_window_seconds=args.sequence_window_seconds,
        sequence_threshold=args.sequence_threshold,
    )
    print_report(report)

    if not args.no_push:
        payload = build_exposition(report, datetime.now(UTC).timestamp())
        try:
            publish(args.pushgateway_url, payload)
        except Exception as exc:  # fail-open: reporting must not crash on a Pushgateway outage
            print(f"WARNING: could not publish to Pushgateway: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
