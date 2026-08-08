#!/usr/bin/env python3
"""Internal-audit mechanism for the NIST AI RMF mapping (GOVERN 1.5, issue #542).

`docs/nist-ai-rmf/governance-policy.md` §9 defines the annual internal audit as
"re-run the assessment behind rmf-mapping.md against current `main` and diff the
statuses". That sentence describes work nobody can repeat identically: re-reading
45 rows by hand produces a different answer per reader and leaves no artifact.

This script makes the mechanical half reproducible. It does *not* re-judge whether
a status is the right one -- that is the auditor's job and requires reading the
system. It checks the things a document can be wrong about without anyone
noticing, and reports what changed since the last audit:

1. **Every backticked file reference resolves.** A row citing
   `scripts/evaluate_retrieval.py` is evidence only while that file exists. A
   rename elsewhere in the repo silently turns cited evidence into a dead
   pointer, and this is the failure mode most likely to go unnoticed between
   audits.
2. **Statuses are drawn from the documented vocabulary.** A row that quietly
   acquires a new status word escapes the convention `rmf-mapping.md`'s own
   header defines.
3. **Referenced issues' states**, when a cached export is supplied (see
   `--issue-states`). A row whose gap is "tracked in #526" reads differently once
   #526 is closed: either the gap closed and the row is stale, or the issue was
   closed without the gap closing, which is worth an auditor's attention. No
   network call is made -- an air-gapped deployment (NFR-1) cannot rely on one,
   and an audit artifact that varies with GitHub's availability is not evidence.
4. **A diff against the previous snapshot**: rows added, rows removed, statuses
   changed, references that broke.

Why the outputs are two files, not one: `--snapshot` writes machine-readable JSON
that the *next* audit diffs against, and `--report` writes the Markdown an
auditor signs and archives. The JSON is the baseline; the Markdown is the
evidence.

Independence (MEASURE 1.3): whoever performs the audit is an organizational
decision this script cannot make. What it removes is the excuse that only the
document's author can perform it -- one command, no repo knowledge, no network.

Usage
-----
    # first run: establish the baseline that later audits diff against
    python scripts/audit_rmf_mapping.py --snapshot docs/nist-ai-rmf/evidence/snapshots/baseline.json

    # an audit: diff against the baseline and write the report to sign
    python scripts/audit_rmf_mapping.py \
        --baseline docs/nist-ai-rmf/evidence/snapshots/baseline.json \
        --report docs/nist-ai-rmf/evidence/snapshots/2026-11-01/internal-audit.md

    # optional: include issue states from a cached export
    gh issue list --state all --limit 700 --json number,state > /tmp/issues.json
    python scripts/audit_rmf_mapping.py --issue-states /tmp/issues.json ...

Exit status: 1 if any cited reference does not resolve or any status is outside
the documented vocabulary (both are defects in the document itself); 0 otherwise.
Status *changes* are reported, never failed on -- a status moving from Gap to
Validated live is the system improving, and an audit that fails on improvement
would just teach people not to run it. `--strict` additionally fails when a
status regresses, for anyone who wants that as a gate.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess  # nosec B404: fixed-argv `git` reads only, see audited_commit()
import sys
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
MAPPING = REPO_ROOT / "docs" / "nist-ai-rmf" / "rmf-mapping.md"
LEDGER = REPO_ROOT / "docs" / "nist-ai-rmf" / "README.md"

# The vocabulary rmf-mapping.md's own header defines. A status is allowed if it
# mentions at least one of these -- rows legitimately combine them
# ("Validated live (append-only); Partial (trace coverage)") because one row can
# group several subcategories, so an exact-match check would be wrong.
STATUS_VOCABULARY = (
    "Validated live",
    "Tested against mocks",
    "Implemented",
    "Gap",
    "TBD",
    "Partial",
    "Draft",
    "N/A",
    "Scoped out",
)

# Ordered worst-to-best, for deciding whether a change is a regression. Rows with
# compound statuses are compared on their weakest mentioned term, since that is
# what an auditor should be looking at.
STATUS_RANK = {
    "Gap": 0,
    "TBD": 1,
    "Draft": 2,
    "Partial": 3,
    "Implemented": 4,
    "Tested against mocks": 5,
    "Validated live": 6,
    "N/A": 7,
    "Scoped out": 7,
}

_TABLE_ROW = re.compile(r"^\|\s*([0-9][^|]*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$", re.M)
_FUNCTION_HEADING = re.compile(r"^## (GOVERN|MAP|MEASURE|MANAGE)\s*$", re.M)
_BACKTICKED = re.compile(r"`([^`]+)`")
_ISSUE_REF = re.compile(r"#(\d+)")
# A backticked span is treated as a file reference only if it looks like one.
# `image:` lines, `--flags`, `rag-clearance:*` role globs and prose in backticks
# are not files, and reporting them as broken references would make the check
# noisy enough to be ignored.
_PATHLIKE = re.compile(r"^[\w./+-]+\.(py|md|yml|yaml|json|toml|tpl|sh|cfg|txt|ini)$")


class Row(NamedTuple):
    function: str
    outcome: str
    evidence: str
    status: str


def parse_mapping(text: str) -> list[Row]:
    """Rows of the mapping table, tagged with the RMF function they sit under."""
    # Where each "## GOVERN"-style heading starts, so a row can be attributed.
    sections = [(m.start(), m.group(1)) for m in _FUNCTION_HEADING.finditer(text)]
    rows: list[Row] = []
    for match in _TABLE_ROW.finditer(text):
        function = "?"
        for start, name in sections:
            if start < match.start():
                function = name
            else:
                break
        rows.append(Row(function, match.group(1), match.group(2), match.group(3)))
    return rows


@functools.lru_cache(maxsize=1)
def tracked_files() -> tuple[str, ...]:
    """Every git-tracked path, which is the only correct search space here.

    The first version of this walked the working tree with `rglob` and excluded
    only `.git` and `node_modules`. That silently resolved bare names into a
    virtualenv: with the `.venv-test` this repo's own CLAUDE.md tells contributors
    to create at the repo root, `metadata.py` resolved to
    `.venv-test/.../site-packages/.../metadata.py` and the run still exited 0.
    Environment-dependent, wrong, and invisible -- the exact failure this check
    exists to catch, in the checker itself. (Found in review by @schuecl; the
    committed baseline.json carried the bad path.)

    Git-tracked is the right space because it is what the audited commit *is*:
    two auditors on the same commit see the same set regardless of what either
    has installed, built, or cached. Falls back to nothing when git is
    unavailable, which surfaces as a broken reference rather than a wrong one.
    """
    try:
        out = subprocess.run(  # nosec B603 B607
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ()
    return tuple(sorted(path for path in out.split("\0") if path))


def resolve_candidates(ref: str) -> list[str]:
    """Every git-tracked path this reference could mean.

    A full path resolves to itself. A bare filename (`claims.py` --- the mapping
    names some modules rather than their paths) matches on basename, and *all*
    matches are returned: picking the first silently invents a citation the
    document did not make, which is how the venv bug stayed invisible.
    """
    tracked = set(tracked_files())
    if ref in tracked:
        return [ref]
    if "/" in ref:
        return []
    return [path for path in tracked_files() if path.rsplit("/", 1)[-1] == ref]


def resolve_reference(ref: str) -> str | None:
    """The single path a reference means, or None if none or several do."""
    candidates = resolve_candidates(ref)
    return candidates[0] if len(candidates) == 1 else None


def file_references(evidence: str) -> list[str]:
    return [ref for ref in _BACKTICKED.findall(evidence) if _PATHLIKE.match(ref)]


def weakest_status(status: str) -> tuple[str, int]:
    """The lowest-ranked vocabulary term a status mentions.

    Matched case-insensitively: rows embed the terms mid-sentence in lowercase
    ("Privacy validated live; fairness scoped out"), and a case-sensitive check
    would report prose style as a documentation defect.
    """
    lowered = status.lower()
    found = [(term, STATUS_RANK[term]) for term in STATUS_RANK if term.lower() in lowered]
    if not found:
        return ("?", -1)
    return min(found, key=lambda pair: pair[1])


def audited_commit() -> dict[str, str]:
    """The commit being audited.

    Recorded rather than "today" so the report says which tree was assessed --
    two auditors running this on the same commit must produce the same artifact,
    which a wall-clock date would break.
    """

    # nosec B603/B607: fixed argv assembled from the literals below -- nothing
    # from argv, the mapping document or the environment reaches these args --
    # no shell, read-only git subcommands, and `git` resolved from PATH the same
    # way every other tool in this repo resolves it. Same pattern and same
    # justification as scripts/adversarial_injection_probe.py's docker calls.
    def git(*args: str) -> str:
        try:
            return subprocess.run(  # nosec B603 B607
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Not a git checkout, or git absent: the audit still runs, it just
            # cannot name the commit. Recording "unknown" is better than
            # refusing to audit, and the report shows it.
            return "unknown"

    return {"commit": git("rev-parse", "HEAD"), "committed": git("log", "-1", "--format=%cs")}


def load_issue_states(path: Path | None) -> dict[str, str]:
    """Issue number -> state, from a `gh issue list --json number,state` export."""
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(entry["number"]): entry["state"].upper() for entry in raw}


def assess(text: str, issue_states: dict[str, str]) -> dict[str, Any]:
    rows = parse_mapping(text)
    assessed = []
    for row in rows:
        refs = file_references(row.evidence)
        candidates = {ref: resolve_candidates(ref) for ref in refs}
        resolved = {ref: (hits[0] if len(hits) == 1 else None) for ref, hits in candidates.items()}
        issues = sorted(set(_ISSUE_REF.findall(row.evidence)), key=int)
        term, rank = weakest_status(row.status)
        assessed.append(
            {
                "function": row.function,
                "outcome": row.outcome,
                "status": row.status,
                "weakest_status": term,
                "status_rank": rank,
                "in_vocabulary": any(v.lower() in row.status.lower() for v in STATUS_VOCABULARY),
                "references": resolved,
                # No tracked file matches: a dead citation.
                "broken_references": sorted(r for r, hits in candidates.items() if not hits),
                # Several do: the document names a file that exists more than
                # once, so which one it means is a guess. Reported rather than
                # guessed -- picking one is what let the venv bug hide.
                "ambiguous_references": {
                    ref: hits for ref, hits in candidates.items() if len(hits) > 1
                },
                "issues": issues,
                "closed_issues": sorted(
                    (i for i in issues if issue_states.get(i) == "CLOSED"), key=int
                ),
            }
        )
    return {"audited": audited_commit(), "rows": assessed}


def diff_snapshots(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    def by_outcome(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {f"{r['function']} {r['outcome']}": r for r in snapshot["rows"]}

    old, new = by_outcome(baseline), by_outcome(current)
    changed = [
        {
            "outcome": key,
            "from": old[key]["status"],
            "to": new[key]["status"],
            "regression": new[key]["status_rank"] < old[key]["status_rank"],
        }
        for key in old.keys() & new.keys()
        if old[key]["status"] != new[key]["status"]
    ]
    newly_broken = [
        {
            "outcome": key,
            "references": sorted(
                set(new[key]["broken_references"]) - set(old[key]["broken_references"])
            ),
        }
        for key in old.keys() & new.keys()
        if set(new[key]["broken_references"]) - set(old[key]["broken_references"])
    ]
    return {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "status_changes": sorted(changed, key=lambda c: c["outcome"]),
        "newly_broken_references": sorted(newly_broken, key=lambda c: c["outcome"]),
    }


def ledger_rows(text: str) -> list[tuple[str, str, str]]:
    """Open-decision ledger rows (number, decision, tracking) from the README.

    §9 names the ledger as an input to the management review, so the audit report
    carries it: the review needs to see which decisions are still open and which
    of their tracking issues have since closed.
    """
    section = text.split("## Open decisions ledger", 1)
    if len(section) < 2:
        return []
    body = section[1].split("\n## ", 1)[0]
    out = []
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].isdigit():
            out.append((cells[0], cells[1], cells[3]))
    return out


def render_report(
    current: dict[str, Any],
    delta: dict[str, Any] | None,
    ledger: list[tuple[str, str, str]],
    issue_states: dict[str, str],
) -> str:
    audited = current["audited"]
    rows = current["rows"]
    broken = [(r["outcome"], r["broken_references"]) for r in rows if r["broken_references"]]
    off_vocab = [r["outcome"] for r in rows if not r["in_vocabulary"]]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["weakest_status"]] = counts.get(row["weakest_status"], 0) + 1

    lines = [
        "# Internal audit — NIST AI RMF mapping",
        "",
        "Generated by `scripts/audit_rmf_mapping.py` (issue #542, GOVERN 1.5). This",
        "file is the mechanical half of the audit: reference integrity, status",
        "vocabulary, and the diff against the previous snapshot. **It is not a",
        "finished audit** until an auditor records their judgement in the sections",
        "marked for it below, and signs.",
        "",
        f"- Audited commit: `{audited['commit']}` (committed {audited['committed']})",
        f"- Mapping rows assessed: {len(rows)}",
        f"- Issue states supplied: {'yes' if issue_states else 'no (references not checked)'}",
        "",
        "## Auditor",
        "",
        "| Field | Value |",
        "|---|---|",
        "| Performed by | _name_ |",
        "| Role / independence from the developer | _to be recorded — MEASURE 1.3_ |",
        "| Date performed | _date_ |",
        "",
        "## 1. Mechanical findings",
        "",
    ]

    if broken:
        lines += [
            "### Broken evidence references",
            "",
            "A cited file that does not exist is not evidence.",
            "",
        ]
        lines += [
            f"- **{outcome}** → {', '.join(f'`{r}`' for r in refs)}" for outcome, refs in broken
        ]
        lines.append("")
    else:
        lines += ["### Broken evidence references", "", "None — every cited file resolves.", ""]

    ambiguous = [
        (r["outcome"], r["ambiguous_references"]) for r in rows if r.get("ambiguous_references")
    ]
    if ambiguous:
        lines += [
            "### Ambiguous evidence references",
            "",
            "The document names a filename that exists at more than one tracked path, "
            "so which file it cites is a guess. Cite the path instead.",
            "",
        ]
        for outcome, refs in ambiguous:
            for ref, hits in refs.items():
                lines.append(
                    f"- **{outcome}** → `{ref}` matches: {', '.join(f'`{h}`' for h in hits)}"
                )
        lines.append("")

    if off_vocab:
        lines += ["### Statuses outside the documented vocabulary", ""]
        lines += [f"- {outcome}" for outcome in off_vocab]
        lines.append("")
    else:
        lines += ["### Status vocabulary", "", "All statuses use the documented terms.", ""]

    lines += [
        "### Status distribution (weakest term per row)",
        "",
        "| Status | Rows |",
        "|---|---|",
    ]
    lines += [
        f"| {status} | {count} |"
        for status, count in sorted(counts.items(), key=lambda kv: STATUS_RANK.get(kv[0], -1))
    ]
    lines.append("")

    lines += ["## 2. Change since the previous audit", ""]
    if delta is None:
        lines += ["No baseline supplied — this run establishes one. Nothing to diff.", ""]
    else:
        if not any(delta.values()):
            lines += ["No rows added or removed, no status changed, no reference broke.", ""]
        if delta["status_changes"]:
            lines += ["### Status changes", "", "| Outcome | From | To | |", "|---|---|---|---|"]
            for change in delta["status_changes"]:
                direction = "**regression**" if change["regression"] else "improvement"
                lines.append(
                    f"| {change['outcome']} | {change['from']} | {change['to']} | {direction} |"
                )
            lines.append("")
        for key, heading in (
            ("added", "Rows added"),
            ("removed", "Rows removed"),
        ):
            if delta[key]:
                lines += [f"### {heading}", ""] + [f"- {o}" for o in delta[key]] + [""]
        if delta["newly_broken_references"]:
            lines += ["### References that broke since the baseline", ""]
            lines += [
                f"- **{c['outcome']}** → {', '.join(f'`{r}`' for r in c['references'])}"
                for c in delta["newly_broken_references"]
            ]
            lines.append("")

    if ledger:
        lines += [
            "## 3. Open-decisions ledger at audit time",
            "",
            "§9 names the ledger as a management-review input. Tracking-issue state is",
            "shown where supplied: a *closed* issue against a still-open decision is",
            "worth checking — either the decision was made and the ledger is stale, or",
            "the issue was closed without the decision being made.",
            "",
            "| # | Decision | Tracking | Issue state |",
            "|---|---|---|---|",
        ]
        for number, decision, tracking in ledger:
            refs = _ISSUE_REF.findall(tracking)
            state = ", ".join(f"#{r}: {issue_states.get(r, 'unknown')}" for r in refs) or "—"
            short = decision if len(decision) <= 90 else decision[:87] + "..."
            lines.append(f"| {number} | {short} | {tracking} | {state} |")
        lines.append("")

    lines += [
        "## 4. Auditor judgement — to be completed by hand",
        "",
        "The checks above cannot tell whether a status is *correct*, only whether the",
        "document is internally consistent. Record here:",
        "",
        "- Rows sampled for substantive re-assessment, and whether the evidence",
        "  supported the claimed status.",
        "- Any status believed wrong, with the reason.",
        "- Findings raised as issues (numbers), and corrective actions with owners.",
        "- Confirmation that the previous audit's findings were addressed.",
        "",
        "## 5. Sign-off",
        "",
        "| Field | Value |",
        "|---|---|",
        "| Auditor signature | _name, date_ |",
        "| Accepted by (accountable AI owner) | _name, date_ |",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mapping", type=Path, default=MAPPING, help="rmf-mapping.md to assess")
    parser.add_argument(
        "--ledger", type=Path, default=LEDGER, help="README.md carrying the open-decisions ledger"
    )
    parser.add_argument("--baseline", type=Path, help="previous snapshot JSON to diff against")
    parser.add_argument("--snapshot", type=Path, help="write the current assessment as JSON here")
    parser.add_argument("--report", type=Path, help="write the Markdown audit report here")
    parser.add_argument(
        "--issue-states", type=Path, help="cached `gh issue list --json number,state` export"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also exit 1 when a status regresses against the baseline",
    )
    args = parser.parse_args(argv)

    if not args.mapping.exists():
        print(f"mapping not found: {args.mapping}", file=sys.stderr)
        return 2

    issue_states = load_issue_states(args.issue_states)
    current = assess(args.mapping.read_text(encoding="utf-8"), issue_states)
    delta = None
    if args.baseline:
        if not args.baseline.exists():
            print(f"baseline not found: {args.baseline}", file=sys.stderr)
            return 2
        delta = diff_snapshots(json.loads(args.baseline.read_text(encoding="utf-8")), current)

    ledger = ledger_rows(args.ledger.read_text(encoding="utf-8")) if args.ledger.exists() else []

    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(
            json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"snapshot written: {args.snapshot}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            render_report(current, delta, ledger, issue_states), encoding="utf-8"
        )
        print(f"report written: {args.report}")

    broken = {
        r["outcome"]: r["broken_references"] for r in current["rows"] if r["broken_references"]
    }
    ambiguous = {
        r["outcome"]: r["ambiguous_references"]
        for r in current["rows"]
        if r.get("ambiguous_references")
    }
    off_vocab = [r["outcome"] for r in current["rows"] if not r["in_vocabulary"]]
    for outcome, refs in broken.items():
        print(f"BROKEN REFERENCE  {outcome}: {', '.join(refs)}", file=sys.stderr)
    for outcome, refs in ambiguous.items():
        for ref, hits in refs.items():
            print(
                f"AMBIGUOUS REFERENCE  {outcome}: `{ref}` matches {len(hits)} tracked "
                f"files ({', '.join(hits[:3])}...) -- cite the path",
                file=sys.stderr,
            )
    for outcome in off_vocab:
        print(f"STATUS OFF-VOCABULARY  {outcome}", file=sys.stderr)
    if delta:
        for change in delta["status_changes"]:
            marker = "REGRESSION" if change["regression"] else "improved"
            print(f"{marker}  {change['outcome']}: {change['from']} -> {change['to']}")

    failed = bool(broken or off_vocab or ambiguous)
    if args.strict and delta and any(c["regression"] for c in delta["status_changes"]):
        failed = True
    if not failed:
        print(f"OK: {len(current['rows'])} rows, every cited reference resolves")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
