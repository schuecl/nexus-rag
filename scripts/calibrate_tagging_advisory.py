"""FR-13/FR-16/FR-30/FR-32: Phase 4 of #138's adaptive tagging-assistance work
(issue #309) -- mine curator corrections already recorded in the audit log and
report, per advisory suggester, how often the curator's final decision agreed
with what was flagged. This is the piece that makes the assistant *adaptive*
rather than a static rulebook: it measures how well Phase 1 (marking-mismatch),
Phase 2 (precedent), and Phase 3 (LLM zero-shot) actually track this
deployment's real curation practice, instead of assuming it.

Data source: every `document.approve`/`document.reject` audit entry already
carries a `tagging_advisory` outcome object in its `detail` column, written by
`_tagging_advisory_outcome()` in services/ingestion-api/app/routes/curate.py
(issue #306 gap 1, extended by #307 and #308) whenever a suggester flagged
something for that document. Nothing new is computed at decision time --
this script only reads what curate.py already wrote.

Why this connects to Postgres directly instead of going through any of the
four services: NFR-2 deliberately makes every application role's own database
credentials INSERT-only on audit_log (see the "NO APPLICATION ROLE GETS SELECT
ON audit_log" note in infra/postgres/apply-service-grants.sh) so that reading
the curation trail is always a distinct, attributable act rather than
something a compromised service could already do. This script authenticates
as its own dedicated, SELECT-only role (`nexus_rag_audit_reporting`,
infra/postgres/ensure-roles.sh) -- run manually or on a schedule (FR-32's
"periodically re-evaluate"), never wired into any service's request path.

What this deliberately does NOT do: refresh a "Phase 2 precedent index".
There isn't one. `VectorStore.find_similar_approved`
(services/common/common/qdrant_backend.py) already queries `status ==
"approved"` directly against live Qdrant on every ingestion -- it reflects the
current corrected/approved corpus with zero staleness, by construction, not
by a batch job. The issue's "refresh precedent from the corrected/approved
set" clause is therefore already true today; there is nothing to build for
it, only to note.

Issue #345: the sensitive-data-pattern advisory family (#342's regex pass,
#343's LLM-assisted pass) has no classification/releasability target to
rank-compare a curator's decision against the way every suggester above does
-- a PII finding says "this text looks sensitive", not "this document should
be tagged X". So `pii_regex`/`pii_llm` below use a different notion of
"agreement" than `Tally.agreement_rate`: whether the curator visibly acted on
the finding at all (rejected the document, or approved it with a changed
classification) versus approved it with the ingestion-time classification
left untouched. A low `acted_on_rate` is a signal worth looking at either way
-- either the finding family is mostly noise curators learn to dismiss, or
genuine spillage-adjacent content is being waved through -- this script only
surfaces the rate, it doesn't judge which.

Issue #380 (calibration follow-on to #378): #378 added an `llm_verdict`
(`likely_false_positive` + rationale) to each Phase 1 regex finding, judged
from context alone -- purely advisory, it never filters a finding. This adds
`pii_regex_llm_verdict` (`PiiVerdictTally`) to measure how well that verdict
actually tracks curation practice: when every finding in a document landed
on the same verdict, did the curator's decision agree? Unlike `pii_regex`/
`pii_llm` above, this has an actual prediction to score against (the
verdict itself), so it uses `agreement_rate` like the classification-tag
suggesters, not `acted_on_rate`. A document where the verdicts disagreed
with each other, or where verification only covered some of its findings,
is counted in `skipped` -- there's no single verdict to score a
document-level curator decision against in that case.

Known limitation, not fixed here: the LLM suggester's `disagrees_with_assigned`
flag (services/ingestion-worker/app/classification_suggestion.py via
_apply_llm_suggestion_advisory) is one boolean covering *either* a
classification disagreement *or* a doc_type disagreement, and the audit
outcome does not separately record the document's doc_type at decision time.
This script can therefore rank-verify and score LLM *classification*
agreement (using `assigned_classification`/`llm_suggested_classification`,
both present in the outcome), but can only report a raw count of LLM
doc_type flags, not an accept/override rate for them -- there is no ground
truth in the audit trail to compare against. A future issue could close this
by having curate.py also record the document's `doc_type` at decision time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_HOST = os.environ.get("POSTGRES_HOST", "postgres")
DEFAULT_PORT = os.environ.get("POSTGRES_PORT", "5432")
DEFAULT_DB = os.environ.get("POSTGRES_DB", "nexus_rag")
DEFAULT_USER = os.environ.get("AUDIT_REPORTING_DB_USER", "nexus_rag_audit_reporting")
DEFAULT_PASSWORD = os.environ.get("AUDIT_REPORTING_DB_PASSWORD", "nexus_rag_audit_reporting")

_DECISION_ACTIONS = ("document.approve", "document.reject")


def default_dsn() -> str:
    override = os.environ.get("AUDIT_REPORTING_DATABASE_URL")
    if override:
        return override
    return (
        f"postgresql://{DEFAULT_USER}:{DEFAULT_PASSWORD}@{DEFAULT_HOST}:{DEFAULT_PORT}/{DEFAULT_DB}"
    )


def fetch_decisions(dsn: str, since: datetime | None) -> list[dict]:
    """Every `document.approve`/`document.reject` audit row, oldest first.

    Fetches the whole `detail` blob rather than filtering on
    `detail->'tagging_advisory'` in SQL: `detail` is a plain JSON column (not
    JSONB, see common/models.py), so a JSON-path predicate would need a cast
    the reporting role has no reason to be granted anything beyond SELECT
    for, and audit_log's decision-action volume is small enough that
    filtering the (already sparse) advisory outcomes in Python is simpler and
    just as correct.

    `psycopg` is imported here, not at module level: this is a scripts/-only
    dependency (scripts/requirements.in) that the repo-root `unit` CI job
    deliberately never installs (same reasoning as `.coveragerc` excluding
    `db.py`/`qdrant_store.py` -- see ci.yml's "minus fastembed/psycopg which
    the unit layer never touches"), and `tests/unit/test_calibrate_tagging_advisory.py`
    only exercises the pure `aggregate()`/`Tally`/history-store logic below, never
    this function -- importing psycopg at module level would make just
    importing this module fail in that job.
    """
    import psycopg

    query = "SELECT action, detail, created_at FROM audit_log WHERE action = ANY(%s)"
    params: list[Any] = [list(_DECISION_ACTIONS)]
    if since is not None:
        query += " AND created_at >= %s"
        params.append(since)
    query += " ORDER BY created_at"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [{"action": r[0], "detail": r[1], "created_at": r[2]} for r in rows]


def fetch_classification_ranks(dsn: str) -> dict[str, int]:
    """value.upper() -> rank for every active ClassificationLevel, so a
    flagged/suggested classification can be rank-compared against the
    curator's final one the same way the worker itself does (lower rank =
    less sensitive, per common/models.py's ClassificationLevel).

    Lazily imports `psycopg` -- see `fetch_decisions`."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT value, rank FROM classification_levels WHERE active = true")
        rows = cur.fetchall()
    return {value.upper(): rank for value, rank in rows}


@dataclass
class Tally:
    """Outcome counts for one suggester's classification-disagreement flag.

    `unresolved` counts a flagged value that no longer appears in the
    currently-configured classification vocabulary (an admin retired or
    renamed it since the decision was made) -- excluded from
    `agreement_rate` since there's no rank to compare against, not silently
    folded into either bucket.
    """

    flagged: int = 0
    accepted: int = 0
    overridden: int = 0
    unresolved: int = 0

    def record(self, *, curator_agreed: bool | None) -> None:
        self.flagged += 1
        if curator_agreed is None:
            self.unresolved += 1
        elif curator_agreed:
            self.accepted += 1
        else:
            self.overridden += 1

    @property
    def agreement_rate(self) -> float | None:
        resolved = self.accepted + self.overridden
        return self.accepted / resolved if resolved else None

    def to_dict(self) -> dict:
        return {
            "flagged": self.flagged,
            "accepted": self.accepted,
            "overridden": self.overridden,
            "unresolved": self.unresolved,
            "agreement_rate": self.agreement_rate,
        }


@dataclass
class PiiTally:
    """Outcome counts for one sensitive-data-pattern advisory (#342 regex /
    #343 LLM-assisted). No classification/releasability target exists to
    rank-compare against (see module docstring), so "agreement" here is
    whether the curator visibly acted on the finding: rejected the document,
    or approved it with a changed classification, versus approving it with
    the ingestion-time classification left untouched.

    `unresolved` counts a flagged document missing `assigned_classification`
    or `final_classification` (e.g. a pre-#345 audit row, or Phase 1's own
    advisory failed to compute for this document) -- there's no way to tell
    approved-unchanged apart from approved-corrected without both, so this
    isn't folded into either bucket.
    """

    flagged: int = 0
    approved_unchanged: int = 0
    approved_corrected: int = 0
    rejected: int = 0
    unresolved: int = 0

    def record(
        self,
        *,
        action: str,
        assigned_classification: str | None,
        final_classification: str | None,
    ) -> None:
        self.flagged += 1
        acted = _pii_curator_acted(action, assigned_classification, final_classification)
        if acted is None:
            self.unresolved += 1
        elif action == "document.reject":
            self.rejected += 1
        elif acted:
            self.approved_corrected += 1
        else:
            self.approved_unchanged += 1

    @property
    def acted_on_rate(self) -> float | None:
        resolved = self.approved_unchanged + self.approved_corrected + self.rejected
        return (self.approved_corrected + self.rejected) / resolved if resolved else None

    def to_dict(self) -> dict:
        return {
            "flagged": self.flagged,
            "approved_unchanged": self.approved_unchanged,
            "approved_corrected": self.approved_corrected,
            "rejected": self.rejected,
            "unresolved": self.unresolved,
            "acted_on_rate": self.acted_on_rate,
        }


def _pii_curator_acted(
    action: str,
    assigned_classification: str | None,
    final_classification: str | None,
) -> bool | None:
    """Shared "did the curator visibly act on this PII finding" signal used
    by both `PiiTally` above and `PiiVerdictTally` below: rejecting the
    document, or approving it with a changed classification, counts as
    acted-on; approving with the ingestion-time classification untouched
    does not. Returns None (unresolved -- see `PiiTally`'s docstring) when
    an approve decision is missing either classification to compare."""
    if action == "document.reject":
        return True
    if assigned_classification is None or final_classification is None:
        return None
    return assigned_classification.upper() != final_classification.upper()


@dataclass
class PiiVerdictTally:
    """Issue #380: calibration for #378's `llm_verdict` -- when the LLM
    verification pass ran and every regex finding in a document landed on
    the *same* verdict (all `likely_false_positive` or all not), did the
    curator's decision agree? A document where the verdicts disagreed with
    each other, or where verification only covered some of the document's
    findings, has no single verdict to score the curator's one document-level
    decision against, so it's counted in `skipped` rather than guessed at.

    "Agreement" mirrors `PiiTally`'s own notion of curator action
    (`_pii_curator_acted`): a `likely_false_positive` verdict agrees with a
    curator who did *not* act on the finding (approved unchanged); a
    not-`likely_false_positive` verdict agrees with a curator who *did*
    (rejected, or approved with a correction). This is a read on how well
    the verdict tracks real curation practice, not a claim about whether the
    finding itself was genuine spillage -- see module docstring.
    """

    predicted_false_positive: int = 0
    predicted_false_positive_agreed: int = 0
    predicted_false_positive_overridden: int = 0
    predicted_genuine: int = 0
    predicted_genuine_agreed: int = 0
    predicted_genuine_overridden: int = 0
    unresolved: int = 0
    skipped: int = 0

    def record(
        self,
        *,
        action: str,
        assigned_classification: str | None,
        final_classification: str | None,
        regex_count: int,
        verified_count: int,
        false_positive_count: int,
    ) -> None:
        if regex_count == 0 or verified_count != regex_count:
            self.skipped += 1
            return
        if false_positive_count == regex_count:
            predicted_false_positive = True
        elif false_positive_count == 0:
            predicted_false_positive = False
        else:
            # Verdicts disagreed with each other across this document's
            # findings -- no single prediction to score.
            self.skipped += 1
            return

        acted = _pii_curator_acted(action, assigned_classification, final_classification)
        if acted is None:
            self.unresolved += 1
            return

        if predicted_false_positive:
            self.predicted_false_positive += 1
            if acted:
                self.predicted_false_positive_overridden += 1
            else:
                self.predicted_false_positive_agreed += 1
        else:
            self.predicted_genuine += 1
            if acted:
                self.predicted_genuine_agreed += 1
            else:
                self.predicted_genuine_overridden += 1

    @property
    def agreement_rate(self) -> float | None:
        agreed = self.predicted_false_positive_agreed + self.predicted_genuine_agreed
        total = self.predicted_false_positive + self.predicted_genuine
        return agreed / total if total else None

    def to_dict(self) -> dict:
        return {
            "predicted_false_positive": self.predicted_false_positive,
            "predicted_false_positive_agreed": self.predicted_false_positive_agreed,
            "predicted_false_positive_overridden": self.predicted_false_positive_overridden,
            "predicted_genuine": self.predicted_genuine,
            "predicted_genuine_agreed": self.predicted_genuine_agreed,
            "predicted_genuine_overridden": self.predicted_genuine_overridden,
            "unresolved": self.unresolved,
            "skipped": self.skipped,
            "agreement_rate": self.agreement_rate,
        }


@dataclass
class Report:
    since: str | None
    generated_at: str
    decisions_considered: int = 0
    decisions_with_advisory: int = 0
    marking_mismatch: Tally = field(default_factory=Tally)
    releasability_caveats: Tally = field(default_factory=Tally)
    precedent: Tally = field(default_factory=Tally)
    llm_classification: Tally = field(default_factory=Tally)
    llm_doc_type_flags: int = 0
    pii_regex: PiiTally = field(default_factory=PiiTally)
    pii_llm: PiiTally = field(default_factory=PiiTally)
    pii_regex_llm_verdict: PiiVerdictTally = field(default_factory=PiiVerdictTally)

    def to_dict(self) -> dict:
        return {
            "since": self.since,
            "generated_at": self.generated_at,
            "decisions_considered": self.decisions_considered,
            "decisions_with_advisory": self.decisions_with_advisory,
            "marking_mismatch": self.marking_mismatch.to_dict(),
            "releasability_caveats": self.releasability_caveats.to_dict(),
            "precedent": self.precedent.to_dict(),
            "llm_classification": self.llm_classification.to_dict(),
            "llm_doc_type_flags": self.llm_doc_type_flags,
            "pii_regex": self.pii_regex.to_dict(),
            "pii_llm": self.pii_llm.to_dict(),
            "pii_regex_llm_verdict": self.pii_regex_llm_verdict.to_dict(),
        }


def _rank(ranks: dict[str, int], value: str | None) -> int | None:
    if not value:
        return None
    return ranks.get(value.upper())


def aggregate(decisions: list[dict], ranks: dict[str, int]) -> Report:
    """Pure aggregation over already-fetched audit rows -- no DB/network I/O,
    so this is unit-testable against constructed rows without a live Postgres.
    """
    report = Report(since=None, generated_at=datetime.now(UTC).isoformat())
    for row in decisions:
        report.decisions_considered += 1
        outcome = (row.get("detail") or {}).get("tagging_advisory")
        if not outcome:
            continue
        report.decisions_with_advisory += 1
        final_rank = _rank(ranks, outcome.get("final_classification"))

        if outcome.get("marking_mismatch_flagged"):
            flagged_rank = _rank(ranks, outcome.get("flagged_classification"))
            if flagged_rank is None or final_rank is None:
                report.marking_mismatch.record(curator_agreed=None)
            else:
                report.marking_mismatch.record(curator_agreed=final_rank >= flagged_rank)

        flagged_caveats = outcome.get("flagged_caveats") or []
        if flagged_caveats:
            final_releasability = set(outcome.get("final_releasability") or [])
            report.releasability_caveats.record(
                curator_agreed=set(flagged_caveats) <= final_releasability
            )

        if "precedent_classification" in outcome:
            precedent_rank = _rank(ranks, outcome.get("precedent_classification"))
            if precedent_rank is None or final_rank is None:
                report.precedent.record(curator_agreed=None)
            else:
                report.precedent.record(curator_agreed=final_rank >= precedent_rank)

        if "llm_suggested_classification" in outcome:
            assigned_rank = _rank(ranks, outcome.get("assigned_classification"))
            suggested_rank = _rank(ranks, outcome.get("llm_suggested_classification"))
            # Mirrors _apply_llm_suggestion_advisory's own classification_disagrees
            # condition: only count this as a classification flag when the
            # suggestion actually outranked what was assigned at ingestion time.
            # `disagrees_with_assigned` can otherwise be true purely from a
            # doc_type mismatch, in which case suggested_rank <= assigned_rank
            # and this isn't a genuine under-classification flag to score.
            if (
                assigned_rank is not None
                and suggested_rank is not None
                and suggested_rank > assigned_rank
            ):
                if final_rank is None:
                    report.llm_classification.record(curator_agreed=None)
                else:
                    report.llm_classification.record(curator_agreed=final_rank >= suggested_rank)

        if outcome.get("llm_suggested_doc_type"):
            report.llm_doc_type_flags += 1

        if "pii_regex_kinds" in outcome:
            report.pii_regex.record(
                action=row.get("action") or "",
                assigned_classification=outcome.get("assigned_classification"),
                final_classification=outcome.get("final_classification"),
            )

        if "pii_regex_llm_verified_count" in outcome:
            report.pii_regex_llm_verdict.record(
                action=row.get("action") or "",
                assigned_classification=outcome.get("assigned_classification"),
                final_classification=outcome.get("final_classification"),
                regex_count=outcome.get("pii_regex_count") or 0,
                verified_count=outcome.get("pii_regex_llm_verified_count") or 0,
                false_positive_count=outcome.get("pii_regex_llm_likely_false_positive_count") or 0,
            )

        if "pii_llm_kinds" in outcome:
            report.pii_llm.record(
                action=row.get("action") or "",
                assigned_classification=outcome.get("assigned_classification"),
                final_classification=outcome.get("final_classification"),
            )

    return report


def persist_report(report: dict, history_dir: Path) -> Path:
    """Write `report` to `history_dir` under a timestamped, sortable filename
    -- the FR-30 "over time" trend store, same shape as
    evaluate_retrieval.py's persist_report."""
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("-", "").replace("+", "_")
    path = history_dir / f"tagging-advisory-calibration-{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    return path


def latest_prior_report(history_dir: Path, exclude: Path | None = None) -> Path | None:
    reports = sorted(
        p for p in history_dir.glob("tagging-advisory-calibration-*.json") if p != exclude
    )
    return reports[-1] if reports else None


def print_report(report: dict) -> None:
    print(f"Tagging-advisory calibration report ({report['generated_at']})")
    if report["since"]:
        print(f"  decisions since: {report['since']}")
    print(f"  decisions considered: {report['decisions_considered']}")
    print(f"  decisions with an advisory flag: {report['decisions_with_advisory']}")
    for name in ("marking_mismatch", "releasability_caveats", "precedent", "llm_classification"):
        tally = report[name]
        rate = tally["agreement_rate"]
        rate_str = f"{rate:.2%}" if rate is not None else "n/a (nothing resolvable)"
        print(
            f"  {name}: flagged={tally['flagged']} accepted={tally['accepted']} "
            f"overridden={tally['overridden']} unresolved={tally['unresolved']} "
            f"agreement_rate={rate_str}"
        )
    print(
        f"  llm_doc_type_flags: {report['llm_doc_type_flags']} "
        "(descriptive count only -- no accept/override ground truth recorded)"
    )
    for name in ("pii_regex", "pii_llm"):
        tally = report[name]
        rate = tally["acted_on_rate"]
        rate_str = f"{rate:.2%}" if rate is not None else "n/a (nothing resolvable)"
        print(
            f"  {name}: flagged={tally['flagged']} "
            f"approved_unchanged={tally['approved_unchanged']} "
            f"approved_corrected={tally['approved_corrected']} rejected={tally['rejected']} "
            f"unresolved={tally['unresolved']} acted_on_rate={rate_str}"
        )
    verdict = report["pii_regex_llm_verdict"]
    verdict_rate = verdict["agreement_rate"]
    verdict_rate_str = (
        f"{verdict_rate:.2%}" if verdict_rate is not None else "n/a (nothing resolvable)"
    )
    print(
        f"  pii_regex_llm_verdict: predicted_false_positive={verdict['predicted_false_positive']} "
        f"(agreed={verdict['predicted_false_positive_agreed']} "
        f"overridden={verdict['predicted_false_positive_overridden']}) "
        f"predicted_genuine={verdict['predicted_genuine']} "
        f"(agreed={verdict['predicted_genuine_agreed']} "
        f"overridden={verdict['predicted_genuine_overridden']}) "
        f"unresolved={verdict['unresolved']} skipped={verdict['skipped']} "
        f"agreement_rate={verdict_rate_str}"
    )


def print_trend(previous: dict, current: dict) -> None:
    print(f"\nCompared to previous report ({previous['generated_at']}):")
    for name in ("marking_mismatch", "releasability_caveats", "precedent", "llm_classification"):
        prev_rate = previous.get(name, {}).get("agreement_rate")
        cur_rate = current[name]["agreement_rate"]
        if prev_rate is None or cur_rate is None:
            print(f"  {name}: not comparable")
            continue
        delta = cur_rate - prev_rate
        print(f"  {name}: {cur_rate:.2%} vs {prev_rate:.2%} (delta {delta:+.2%})")
    for name in ("pii_regex", "pii_llm"):
        prev_rate = previous.get(name, {}).get("acted_on_rate")
        cur_rate = current[name]["acted_on_rate"]
        if prev_rate is None or cur_rate is None:
            print(f"  {name}: not comparable")
            continue
        delta = cur_rate - prev_rate
        print(f"  {name}: {cur_rate:.2%} vs {prev_rate:.2%} (delta {delta:+.2%})")
    prev_verdict_rate = previous.get("pii_regex_llm_verdict", {}).get("agreement_rate")
    cur_verdict_rate = current["pii_regex_llm_verdict"]["agreement_rate"]
    if prev_verdict_rate is None or cur_verdict_rate is None:
        print("  pii_regex_llm_verdict: not comparable")
    else:
        delta = cur_verdict_rate - prev_verdict_rate
        print(
            f"  pii_regex_llm_verdict: {cur_verdict_rate:.2%} vs {prev_verdict_rate:.2%} "
            f"(delta {delta:+.2%})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="only mine decisions at or after this ISO 8601 timestamp (default: all history)",
    )
    parser.add_argument("--dsn", type=str, default=None, help="override the Postgres DSN")
    parser.add_argument(
        "--output", type=Path, default=None, help="also write the JSON report to this path"
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="persist this run's JSON report here under a timestamped name (FR-30 trend "
        "store); if set, also prints an informational trend line against the most recent "
        "prior report in this directory",
    )
    parser.add_argument(
        "--min-agreement",
        type=float,
        default=None,
        help="exit non-zero if any suggester's agreement_rate drops below this fraction "
        "(opt-in -- unset by default, since a curator override is not, by itself, proof the "
        "suggester was wrong; use this only if this deployment wants a hard floor)",
    )
    args = parser.parse_args()

    import psycopg

    since = datetime.fromisoformat(args.since) if args.since else None
    dsn = args.dsn or default_dsn()

    try:
        decisions = fetch_decisions(dsn, since)
        ranks = fetch_classification_ranks(dsn)
    except psycopg.OperationalError as exc:
        print(f"FAILED: could not connect to Postgres: {exc}", file=sys.stderr)
        sys.exit(1)

    report = aggregate(decisions, ranks)
    report.since = args.since
    report_dict = report.to_dict()
    print_report(report_dict)

    saved: Path | None = None
    if args.history_dir:
        saved = persist_report(report_dict, args.history_dir)
        print(f"\nPersisted report to {saved}")
        previous_path = latest_prior_report(args.history_dir, exclude=saved)
        if previous_path is not None:
            print_trend(json.loads(previous_path.read_text()), report_dict)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report_dict, indent=2))
        print(f"Wrote report to {args.output}")

    if args.min_agreement is not None:
        suggesters = (
            "marking_mismatch",
            "releasability_caveats",
            "precedent",
            "llm_classification",
            "pii_regex_llm_verdict",
        )
        below_floor = [
            name
            for name in suggesters
            if (rate := report_dict[name]["agreement_rate"]) is not None
            and rate < args.min_agreement
        ]
        if below_floor:
            print(
                f"\nFAILED: agreement_rate below --min-agreement={args.min_agreement} for: "
                f"{', '.join(below_floor)}",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
