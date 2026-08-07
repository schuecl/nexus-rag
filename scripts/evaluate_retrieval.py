"""FR-30/FR-32: fixed golden-query regression harness for retrieval quality.
Runs each query in golden_queries.json through the real rag_search pipeline
(via orchestration-mcp's debug endpoint) and computes recall@K, precision@K,
and first-relevant-rank against the expected documents -- plus rank-aware
MRR/nDCG@K/precision@k (advisory, issue #514: set-membership recall cannot
see an ordering regression, which is what fusion and the reranker exist to
prevent), a config fingerprint identifying the model/chunking/golden-set
configuration each persisted report was measured under, and a hard FR-26
check that no unapproved (pending/rejected/superseded) chunk is ever returned,
regardless of the querying persona's clearance. Any query below full recall@K
fails the run (issue #397, `--fail-on-miss`), as does any FR-26 leak.

Issue #514 widens the set beyond clean admin-persona queries: typo and
vague/multi-part phrasings (how operators actually type), a no-relevant-doc
abstention query feeding a `mean_abstention_noise` advisory metric, and a
`--persona-set` file of cases that each name their own querying persona, so
recall and the FR-26 leak check run under multiple claims sets -- including
bob-query/carol-curator, for whom the Signal-Corps-scoped SECRET document
must be excluded by the access-scope leg of the mandatory filter.

Issue #397: the original five queries are short and keyword-heavy, and in the
7-document dev corpus every one of them saturates at recall 1.0 through the
BM25 leg alone (with only ~4 retrievable documents and top_k=5, the candidate
set is the whole corpus). Two paraphrase-style queries with top_k=2 and zero
content-word overlap with their target give the harness dense-leg headroom:
BM25 scores nothing for them, so only dense similarity can rank the target
into the top 2, and a dense-leg regression (e.g. a broken embedding prefix)
becomes a visible recall miss instead of disappearing under fusion.

That check is done by inspecting each returned chunk's own `status` payload
field, not by matching golden_queries.json's `forbid` filenames against
`returned` filenames (issue #226). Filename is user-supplied and explicitly
not unique (see CLAUDE.md's data model section) -- two documents can share a
filename while in different states, and a name-based check has no way to
tell a legitimately-returned approved copy from an unrelated forbidden one
sharing its name. The retrieval-time access filter
(common/qdrant_filters.py) already guarantees `status == "approved"` on both
legs of hybrid search before a chunk can be returned at all, so asserting
that directly on every result is both simpler and immune to name collisions
-- it inspects the identity of the chunk actually returned instead of
comparing names. `forbid` is kept and still checked, but only as an
informational content-overlap note (`content_overlap` below): it can flag
a false positive whenever the corpus has a duplicate filename, so it must
never be the thing that fails the build.

This remains deliberately judge-free: it is the fast, deterministic gate for
FR-30's literal "recall@K, precision@K, or an equivalent proxy" and FR-26's
approved-only invariant. Issue #74's `evaluate_rag_quality.py` complements it
with a local LLM judge and the real LibreChat generation path for contextual
relevancy/recall/precision, faithfulness, answer quality, and citation
validity. Those noisy judge scores are relative tuning signals, not a
replacement for this deterministic safety check.

Run manually or on a schedule (FR-32's "periodically re-evaluate") --
`docker compose --profile eval run --rm eval-retrieval`, or directly with
Python once services are reachable. See docs/dev-setup.md.

FR-30's "over time" clause: pass `--history-dir` to persist each run's report
under a timestamped filename (the trend store), and `--baseline` (or the
auto-selected latest prior report in the history dir) to diff this run's
recall@K/precision@K against it and fail on regression. That is what makes a
quality drop visible rather than silent. See docs/testing.md for the
re-evaluation trigger policy (mandatory on any embedding/reranker model pin
change, NFR-16).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from _keycloak import get_token

ORCHESTRATION_MCP_URL = os.environ.get("ORCHESTRATION_MCP_URL", "http://orchestration-mcp:8002")
DEFAULT_GOLDEN_SET = Path(__file__).parent / "golden_queries.json"
DEFAULT_PERSONA_SET = Path(__file__).parent / "golden_queries_personas.json"
# Broadest-access persona by default, so the metrics measure ranking quality
# rather than being confounded by this user's own clearance/org scoping.
EVAL_PERSONA = os.environ.get("EVAL_PERSONA", "dave-admin")


def run_query(token: str, query: str, top_k: int) -> list[dict]:
    resp = httpx.post(
        f"{ORCHESTRATION_MCP_URL}/debug/rag_search",
        params={"query": query, "top_k": top_k},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return [
        {
            "filename": r["payload"]["filename"],
            "document_id": r["payload"].get("document_id"),
            "status": r["payload"].get("status"),
        }
        for r in body.get("results", [])
    ]


# Fixed rank cutoffs for precision@k, evaluated only where a query's own
# top_k reaches them (the paraphrase queries run at top_k=2, where a
# precision@5 would be arithmetically guaranteed to look like a miss).
_PRECISION_CUTOFFS = (1, 3, 5)

# Environment knobs that change what the pipeline retrieves or how it ranks.
# They live on the *services* (and, for the chunking pair, on what was in
# effect at ingestion time); the eval container mirrors them via
# docker-compose.yml so the fingerprint records the configuration a run's
# numbers belong to. Unset and empty are recorded identically as null --
# both mean "feature at its default".
_FINGERPRINT_ENV_VARS = (
    "EMBEDDING_MODEL",
    "RERANKER_MODEL",
    "RERANK_SCORE_FLOOR",
    "CONTENT_TYPE_BOOSTS",
    "CHUNK_TARGET_WORDS",
    "CHUNK_OVERLAP_RATIO",
)


def config_fingerprint(golden_set: list[dict], persona: str) -> dict:
    """The configuration identity of a run, for baseline comparability.

    Comparing runs across a config change (different embedding model, reranker
    floor, chunking, golden set, or persona) is the main reason to benchmark --
    but a silent config difference makes a delta unattributable. The full
    ``config`` dict is stored in every persisted report; the ``fingerprint``
    digest is what `compare_to_baseline` checks to annotate (not fail) a
    cross-config comparison.
    """
    config: dict[str, str | None] = {
        name: (os.environ.get(name) or None) for name in _FINGERPRINT_ENV_VARS
    }
    golden_canonical = json.dumps(golden_set, sort_keys=True)
    config["golden_set_sha256"] = hashlib.sha256(golden_canonical.encode()).hexdigest()
    config["persona"] = persona
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return {"config": config, "fingerprint": digest}


def rank_metrics(returned: list[str], expect: list[str], top_k: int) -> dict:
    """Rank-aware scores for one query: reciprocal rank, nDCG@K, precision@k.

    Recall/precision over the whole top-K are blind to ordering -- the target
    document at rank 1 and rank 5 score identically, yet ordering is exactly
    what RRF fusion and the reranker exist to improve. These metrics make an
    ordering regression visible.

    Relevance is binary (returned filename is in `expect`), and each expected
    filename earns credit once at its first occurrence: filenames are not
    unique in the corpus (issue #226), so without that cap a duplicate could
    inflate DCG above the ideal. Abstention queries (empty `expect`) score
    None throughout -- their contract is the FR-26 leak check.
    """
    if not expect:
        return {"reciprocal_rank": None, "ndcg_at_k": None, "precision_at": {}}

    credited: set[str] = set()
    gains: list[float] = []
    for name in returned:
        hit = name in expect and name not in credited
        if hit:
            credited.add(name)
        gains.append(1.0 if hit else 0.0)

    first_hit = next((i + 1 for i, g in enumerate(gains) if g), None)
    reciprocal_rank = (1.0 / first_hit) if first_hit else 0.0

    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal_dcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(expect), top_k)))
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0

    # Short result lists (relevance floor, small corpus) divide by k, not by
    # len(returned): an empty tail holds no relevant document by definition.
    precision_at = {str(k): sum(gains[:k]) / k for k in _PRECISION_CUTOFFS if k <= top_k}
    return {"reciprocal_rank": reciprocal_rank, "ndcg_at_k": ndcg, "precision_at": precision_at}


def evaluate(golden_set: list[dict], token_for: Callable[[str], str], persona: str) -> dict:
    """Score every golden case; `persona` is the default identity, and a case
    may override it with its own ``persona`` field (issue #514's coverage
    matrix -- the same pipeline answers differently per claims set, and the
    FR-26 leak check must hold under each of them, not just the broadest one).
    `token_for` maps a persona name to a bearer token; main() passes a
    memoizing wrapper around the Keycloak password grant.
    """
    per_query = []
    for case in golden_set:
        top_k = case.get("top_k", 5)
        case_persona = case.get("persona", persona)
        results = run_query(token_for(case_persona), case["query"], top_k)
        returned = [r["filename"] for r in results]
        expect = case.get("expect", [])
        forbid = case.get("forbid", [])

        found = [f for f in expect if f in returned]
        recall = (len(found) / len(expect)) if expect else None
        precision = (len(found) / len(returned)) if (expect and returned) else None
        rank = next((i + 1 for i, f in enumerate(returned) if f in expect), None)
        # Hard FR-26 check: the access filter guarantees only `approved`
        # chunks can ever be returned, so any other status here means the
        # filter itself was bypassed -- unambiguous regardless of filename.
        unapproved = [r for r in results if r["status"] != "approved"]
        # Informational only (see module docstring): a forbidden filename
        # appearing among approved, unrelated results is not a leak.
        overlap = [f for f in forbid if f in returned]

        per_query.append(
            {
                "query": case["query"],
                "persona": case_persona,
                "returned": returned,
                "expect": expect,
                "forbid": forbid,
                "recall_at_k": recall,
                "precision_at_k": precision,
                "first_relevant_rank": rank,
                **rank_metrics(returned, expect, top_k),
                "unapproved_leaks": unapproved,
                "content_overlap": overlap,
                "note": case.get("note"),
            }
        )

    recalls = [q["recall_at_k"] for q in per_query if q["recall_at_k"] is not None]
    precisions = [q["precision_at_k"] for q in per_query if q["precision_at_k"] is not None]
    rrs = [q["reciprocal_rank"] for q in per_query if q["reciprocal_rank"] is not None]
    ndcgs = [q["ndcg_at_k"] for q in per_query if q["ndcg_at_k"] is not None]
    total_leaks = sum(len(q["unapproved_leaks"]) for q in per_query)
    # Issue #514: what comes back when nothing should? Mean returned-count
    # over the abstention queries (empty `expect`). Advisory and *not* in the
    # baseline comparison: lower is better, which inverts the drop-means-
    # regression arithmetic every compared metric shares.
    abstention_counts = [len(q["returned"]) for q in per_query if not q["expect"]]

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "persona": persona,
        **config_fingerprint(golden_set, persona),
        "mean_recall_at_k": (sum(recalls) / len(recalls)) if recalls else None,
        "mean_precision_at_k": (sum(precisions) / len(precisions)) if precisions else None,
        "mean_reciprocal_rank": (sum(rrs) / len(rrs)) if rrs else None,
        "mean_ndcg_at_k": (sum(ndcgs) / len(ndcgs)) if ndcgs else None,
        "mean_abstention_noise": (
            (sum(abstention_counts) / len(abstention_counts)) if abstention_counts else None
        ),
        "total_forbidden_leaks": total_leaks,
        "queries": per_query,
    }


def print_report(report: dict) -> None:
    print(f"Retrieval evaluation @ {report['timestamp']} (persona={report['persona']})")
    print(f"  mean recall@K:    {report['mean_recall_at_k']}")
    print(f"  mean precision@K: {report['mean_precision_at_k']}")
    print(f"  mean MRR:         {report['mean_reciprocal_rank']}")
    print(f"  mean nDCG@K:      {report['mean_ndcg_at_k']}")
    print(f"  abstention noise: {report['mean_abstention_noise']}")
    print(f"  config:           {report['fingerprint'][:12]}")
    print(f"  forbidden leaks:  {report['total_forbidden_leaks']}")
    for q in report["queries"]:
        status = "OK"
        if q["expect"] and not any(f in q["returned"] for f in q["expect"]):
            status = "MISS"
        if q["content_overlap"] and not q["unapproved_leaks"]:
            status = "OVERLAP"
        if q["unapproved_leaks"]:
            status = "LEAK"
        note = f"  ({q['note']})" if q["note"] else ""
        who = f" [{q['persona']}]" if q["persona"] != report["persona"] else ""
        print(f"  [{status}]{who} {q['query']!r} -> {q['returned']}{note}")
        if q["content_overlap"]:
            print(
                f"    content_overlap (informational, not a leak -- see #226): "
                f"{q['content_overlap']}"
            )
        if q["unapproved_leaks"]:
            print(f"    unapproved_leaks: {q['unapproved_leaks']}")


def persist_report(report: dict, history_dir: Path) -> Path:
    """Write `report` to `history_dir` under a timestamped, sortable filename.

    The accumulated directory of these files is FR-30's trend store: each run is
    kept rather than overwritten, so retrieval quality over time is inspectable
    instead of being a single point-in-time snapshot that makes degradation
    "silent". Returns the path written.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    # Colons aren't portable in filenames; strip the ISO separators but keep the
    # stamp lexicographically sortable so latest_prior_report() can pick newest.
    stamp = report["timestamp"].replace(":", "").replace("-", "")
    path = history_dir / f"retrieval-eval-{stamp}.json"
    path.write_text(json.dumps(report, indent=2))
    return path


def latest_prior_report(history_dir: Path, exclude: Path | None = None) -> Path | None:
    """The most recent persisted report in `history_dir`, excluding `exclude`
    (typically the run just written). Filenames are timestamp-sortable, so the
    last by name is the newest.
    """
    reports = sorted(p for p in history_dir.glob("retrieval-eval-*.json") if p != exclude)
    return reports[-1] if reports else None


def recall_misses(report: dict) -> list[str]:
    """Queries that failed to return every expected document (recall@K < 1.0).

    This is what e2e.yml has always described as "fails on any recall miss
    against the golden set" -- but until issue #397 nothing actually exited
    non-zero on a miss: main() only failed on forbidden leaks and baseline
    regressions, and the compose eval run passes no baseline. The gap was
    invisible while every golden query saturated at recall 1.0 through BM25
    alone; the paraphrase queries added for #397 are only worth having if a
    miss on them fails the run.

    Abstention queries (empty `expect`, recall None) are never misses -- their
    contract is the FR-26 leak check, not recall.
    """
    return [
        q["query"]
        for q in report["queries"]
        if q["recall_at_k"] is not None and q["recall_at_k"] < 1.0
    ]


# The headline quality metrics a regression is judged on. The forbidden-leak
# count is handled separately -- any leak is a hard FR-26 failure, not a
# tolerance-gated regression.
_COMPARED_METRICS = ("mean_recall_at_k", "mean_precision_at_k")

# Rank-aware metrics reported in every comparison but never failing it:
# advisory until enough persisted baselines exist to know their run-to-run
# noise, per docs/testing.md's gated-vs-advisory convention. Promotion to
# _COMPARED_METRICS is the whole change once that trend data is in.
_ADVISORY_METRICS = ("mean_reciprocal_rank", "mean_ndcg_at_k")


def compare_to_baseline(
    current: dict,
    baseline: dict,
    tolerance: float = 0.0,
    allow_config_change: bool = False,
) -> dict:
    """Diff `current`'s headline metrics against `baseline` (FR-30/FR-32).

    A metric regresses when it drops more than `tolerance` below the baseline.
    A metric that is None on either side (no scored queries that run, or a
    baseline written before the metric existed) is reported but never counted
    as a regression. `tolerance` absorbs benign run-to-run noise; set it to 0
    to fail on any decrease. `_ADVISORY_METRICS` are diffed the same way but
    can never set `regressed`.

    `config_mismatch` is True when both reports carry a config fingerprint
    and they differ: the comparison still runs (a cross-config diff is often
    the point), but the deltas measure the config change too, so a regression
    verdict against such a baseline needs a human eye rather than silence.
    A baseline predating fingerprints compares as None -> not a mismatch.
    """
    metrics: dict[str, dict] = {}
    regressed = False
    for name in _COMPARED_METRICS + _ADVISORY_METRICS:
        advisory = name in _ADVISORY_METRICS
        cur = current.get(name)
        base = baseline.get(name)
        if cur is None or base is None:
            metrics[name] = {
                "current": cur,
                "baseline": base,
                "delta": None,
                "regressed": False,
                "advisory": advisory,
            }
            continue
        delta = cur - base
        is_regression = delta < -tolerance
        regressed = regressed or (is_regression and not advisory)
        metrics[name] = {
            "current": cur,
            "baseline": base,
            "delta": delta,
            "regressed": is_regression,
            "advisory": advisory,
        }
    cur_fp = current.get("fingerprint")
    base_fp = baseline.get("fingerprint")
    return {
        "regressed": regressed,
        "tolerance": tolerance,
        "baseline_timestamp": baseline.get("timestamp"),
        "config_mismatch": bool(cur_fp and base_fp and cur_fp != base_fp),
        "config_change_allowed": bool(allow_config_change),
        "metrics": metrics,
    }


def print_comparison(comparison: dict, baseline_path: Path) -> None:
    print(
        f"\nBaseline comparison vs {baseline_path} "
        f"(@ {comparison['baseline_timestamp']}, tolerance={comparison['tolerance']}):"
    )
    if comparison["config_mismatch"]:
        print(
            "  WARNING: config fingerprints differ -- these deltas include the "
            "config change (model/chunking/golden-set/persona), not just drift"
        )
    for name, m in comparison["metrics"].items():
        if m["delta"] is None:
            print(f"  {name}: {m['current']} (baseline {m['baseline']}) -- not comparable")
            continue
        if m["regressed"]:
            verdict = "advisory regression" if m["advisory"] else "REGRESSION"
        else:
            verdict = "ok"
        print(
            f"  {name}: {m['current']:.4f} vs {m['baseline']:.4f} "
            f"(delta {m['delta']:+.4f}) [{verdict}]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument(
        "--output", type=Path, default=None, help="also write the JSON report to this path"
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="persist this run's JSON report here under a timestamped name (FR-30 trend "
        "store). When set and --baseline is not given, the most recent prior report in this "
        "directory is used as the baseline.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="compare this run's metrics against this JSON report and fail on regression "
        "(FR-32); overrides the baseline auto-selected from --history-dir",
    )
    parser.add_argument(
        "--regression-tolerance",
        type=float,
        default=0.0,
        help="allowed drop in a mean metric before it counts as a regression (default 0.0)",
    )
    parser.add_argument(
        "--allow-config-change",
        action="store_true",
        default=False,
        help="permit a baseline comparison whose config fingerprint differs from this "
        "run's (issue #525). Without it a mismatch fails closed: the deltas would "
        "measure the config change as well as any quality change, so a regression "
        "verdict against such a baseline is not attributable. The override is stamped "
        "into the comparison as config_change_allowed so a report cannot look like a "
        "same-config comparison after the fact.",
    )
    parser.add_argument(
        "--fail-on-regression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="exit non-zero if a metric regresses against the baseline (default: enabled)",
    )
    parser.add_argument(
        "--fail-on-miss",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="exit non-zero if any golden query returns less than full recall@K "
        "(default: enabled -- this is the documented e2e contract)",
    )
    parser.add_argument(
        "--persona-set",
        type=Path,
        default=DEFAULT_PERSONA_SET,
        help="additional golden cases that each name their own querying persona (issue "
        "#514 coverage matrix). Kept out of golden_queries.json because "
        "evaluate_rag_quality.py runs that file through LibreChat as a single user, where "
        "per-case personas would score nonsense. Pass a non-existent path to skip.",
    )
    args = parser.parse_args()

    golden_set = json.loads(args.golden_set.read_text())
    if args.persona_set and args.persona_set.exists():
        golden_set = golden_set + json.loads(args.persona_set.read_text())

    tokens: dict[str, str] = {}

    def token_for(persona: str) -> str:
        if persona not in tokens:
            tokens[persona] = get_token(persona)
        return tokens[persona]

    report = evaluate(golden_set, token_for, EVAL_PERSONA)
    print_report(report)

    saved: Path | None = None
    if args.history_dir:
        saved = persist_report(report, args.history_dir)
        print(f"\nPersisted report to {saved}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"Wrote report to {args.output}")

    baseline_path = args.baseline
    if baseline_path is None and args.history_dir:
        baseline_path = latest_prior_report(args.history_dir, exclude=saved)

    regressed = False
    config_blocked = False
    if baseline_path and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
        comparison = compare_to_baseline(
            report,
            baseline,
            args.regression_tolerance,
            allow_config_change=args.allow_config_change,
        )
        print_comparison(comparison, baseline_path)
        regressed = comparison["regressed"]
        config_blocked = comparison["config_mismatch"] and not args.allow_config_change
    elif args.baseline:
        print(
            f"\nBaseline {args.baseline} not found -- skipping regression check",
            file=sys.stderr,
        )

    failed = False
    misses = recall_misses(report)
    if misses and args.fail_on_miss:
        print(
            f"\nFAILED: {len(misses)} golden quer{'y' if len(misses) == 1 else 'ies'} "
            f"missed expected documents (recall@K < 1.0): {misses}",
            file=sys.stderr,
        )
        failed = True
    if report["total_forbidden_leaks"] > 0:
        print(
            "\nFAILED: forbidden (unapproved/rejected/superseded) content leaked into "
            "results -- this is a FR-26 regression, not just a quality miss",
            file=sys.stderr,
        )
        failed = True
    if config_blocked:
        # Issue #525: fail closed rather than reporting an unattributable verdict.
        # A cross-config diff is sometimes exactly what someone wants, which is why
        # there is an override -- but it has to be asked for, because the alternative
        # is a green run that silently compared two different systems, or a red one
        # blamed on quality when a model changed underneath it.
        print(
            "\nFAILED: the baseline's config fingerprint differs from this run's, so "
            "its deltas measure the configuration change as well as any quality change "
            "(issue #525). Re-run with --allow-config-change to compare anyway (the "
            "override is stamped into the report), or point --baseline at a report from "
            "the same configuration.",
            file=sys.stderr,
        )
        failed = True
    if regressed and args.fail_on_regression:
        print(
            "\nFAILED: retrieval quality regressed against the baseline (FR-30/FR-32)",
            file=sys.stderr,
        )
        failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
