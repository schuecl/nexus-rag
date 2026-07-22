"""FR-30/FR-32: fixed golden-query regression harness for retrieval quality.
Runs each query in golden_queries.json through the real rag_search pipeline
(via orchestration-mcp's debug endpoint) and computes recall@K, precision@K,
and first-relevant-rank against the expected documents -- plus a separate
"forbidden" check that pending/rejected/superseded content never leaks into
results regardless of the querying persona's clearance, which is as much a
security regression check (FR-26) as a quality one.

Deliberately not the `ragas` library itself (REQUIREMENTS.md Section 7.6):
RAGAS's more interesting metrics (faithfulness, LLM-judged context
precision/recall) need a configured LLM judge and a wired-up generation
step, neither of which exists in this repo yet -- generation happens via
LibreChat/LiteLLM, outside this codebase. This is a lighter,
judge-free stand-in that covers FR-30's literal "recall@K, precision@K, or
an equivalent proxy" using only the retrieval layer this repo controls.
Revisit once end-to-end generation is wired and a judge model is available.

Run manually or on a schedule (FR-32's "periodically re-evaluate") --
`docker compose --profile eval run --rm eval-retrieval`, or directly with
Python once services are reachable. See docs/dev-setup.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from _keycloak import get_token

ORCHESTRATION_MCP_URL = os.environ.get("ORCHESTRATION_MCP_URL", "http://orchestration-mcp:8002")
DEFAULT_GOLDEN_SET = Path(__file__).parent / "golden_queries.json"
# Broadest-access persona by default, so the metrics measure ranking quality
# rather than being confounded by this user's own clearance/org scoping.
EVAL_PERSONA = os.environ.get("EVAL_PERSONA", "dave-admin")


def run_query(token: str, query: str, top_k: int) -> list[str]:
    resp = httpx.post(
        f"{ORCHESTRATION_MCP_URL}/debug/rag_search",
        params={"query": query, "top_k": top_k},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return [r["payload"]["filename"] for r in body.get("results", [])]


def evaluate(golden_set: list[dict], token: str, persona: str) -> dict:
    per_query = []
    for case in golden_set:
        top_k = case.get("top_k", 5)
        returned = run_query(token, case["query"], top_k)
        expect = case.get("expect", [])
        forbid = case.get("forbid", [])

        found = [f for f in expect if f in returned]
        recall = (len(found) / len(expect)) if expect else None
        precision = (len(found) / len(returned)) if (expect and returned) else None
        rank = next((i + 1 for i, f in enumerate(returned) if f in expect), None)
        leaked = [f for f in forbid if f in returned]

        per_query.append(
            {
                "query": case["query"],
                "returned": returned,
                "expect": expect,
                "forbid": forbid,
                "recall_at_k": recall,
                "precision_at_k": precision,
                "first_relevant_rank": rank,
                "leaked_forbidden": leaked,
                "note": case.get("note"),
            }
        )

    recalls = [q["recall_at_k"] for q in per_query if q["recall_at_k"] is not None]
    precisions = [q["precision_at_k"] for q in per_query if q["precision_at_k"] is not None]
    total_leaks = sum(len(q["leaked_forbidden"]) for q in per_query)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "persona": persona,
        "mean_recall_at_k": (sum(recalls) / len(recalls)) if recalls else None,
        "mean_precision_at_k": (sum(precisions) / len(precisions)) if precisions else None,
        "total_forbidden_leaks": total_leaks,
        "queries": per_query,
    }


def print_report(report: dict) -> None:
    print(f"Retrieval evaluation @ {report['timestamp']} (persona={report['persona']})")
    print(f"  mean recall@K:    {report['mean_recall_at_k']}")
    print(f"  mean precision@K: {report['mean_precision_at_k']}")
    print(f"  forbidden leaks:  {report['total_forbidden_leaks']}")
    for q in report["queries"]:
        status = "OK"
        if q["expect"] and not any(f in q["returned"] for f in q["expect"]):
            status = "MISS"
        if q["leaked_forbidden"]:
            status = "LEAK"
        note = f"  ({q['note']})" if q["note"] else ""
        print(f"  [{status}] {q['query']!r} -> {q['returned']}{note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument(
        "--output", type=Path, default=None, help="also write the JSON report to this path"
    )
    args = parser.parse_args()

    golden_set = json.loads(args.golden_set.read_text())
    token = get_token(EVAL_PERSONA)
    report = evaluate(golden_set, token, EVAL_PERSONA)
    print_report(report)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"\nWrote report to {args.output}")

    if report["total_forbidden_leaks"] > 0:
        print("\nFAILED: forbidden (unapproved/rejected/superseded) content leaked into "
              "results -- this is a FR-26 regression, not just a quality miss", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
