"""Issue #431 (#394 follow-up): reproducible calibration pass for
RERANK_SCORE_FLOOR against golden_queries.json.

#394 added the floor as an off-by-default mechanism with a starting-point
value picked from an ad hoc measurement, explicitly flagged in the CHANGELOG
as needing a real calibration run before anyone should turn it on. This
script is that calibration run, made repeatable: for every golden query it
fetches the *unfiltered* rerank score of each candidate directly from
reranker-service (bypassing the floor so nothing is hidden), grouped by
whether the query is expected to abstain (`expected_abstention: true`) or
answer.

What "correct" looks like for a floor value `F`:
  - every answerable query's expected document must score >= F (otherwise
    the floor starts eating real answers -- a false-negative regression the
    fixed golden-query harness in evaluate_retrieval.py already gates on
    with --fail-on-miss)
  - every abstention query's best (wrong) candidate must score < F
    (otherwise the floor never fires for the case it exists to catch)

That gives a closed interval (worst abstention score, best-hard-answerable
score] that any valid floor must sit inside -- this script prints both
boundaries and flags whether a candidate value clears them. It does not
pick a value for you: see docs/testing.md's "Calibrating RERANK_SCORE_FLOOR"
section for how the printed interval was turned into the shipped default.

Requires the dev stack up with DEBUG_RAG_SEARCH_ENABLED=true (default) and,
importantly, a *freshly seeded* corpus -- a long-running dev stack that has
accumulated extra documents from other scripts (e.g.
adversarial_injection_probe.py) will skew the scores measured here away from
what golden_queries.json was designed against. `docker compose down -v &&
docker compose up --build` before running this reseeds the clean 7-document
corpus.

Usage: ORCHESTRATION_MCP_URL=... KEYCLOAK_URL=... RERANKER_URL=... \
    RERANKER_SHARED_SECRET=... python3 scripts/calibrate_rerank_floor.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from _keycloak import get_token

ORCHESTRATION_MCP_URL = os.environ.get("ORCHESTRATION_MCP_URL", "http://orchestration-mcp:8002")
RERANKER_URL = os.environ.get("RERANKER_URL", "http://reranker-service:8003")
RERANKER_SHARED_SECRET = os.environ.get("RERANKER_SHARED_SECRET", "")
DEFAULT_GOLDEN_SET = Path(__file__).parent / "golden_queries.json"
EVAL_PERSONA = os.environ.get("EVAL_PERSONA", "dave-admin")


def scored_candidates(query: str, top_k: int, token: str) -> list[tuple[float, str]]:
    """Fetch the full hybrid candidate pool for `query` (floor disabled at
    the caller's own risk -- this reads whatever the server is currently
    configured with, so run this against a stack with RERANK_SCORE_FLOOR
    unset) and re-score it directly against reranker-service, matching
    rag_search.py's own hybrid_limit = max(top_k * 4, 20) pool sizing so the
    candidate set here is the same one production reranks. Returns
    (score, filename) sorted best-first.
    """
    resp = httpx.post(
        f"{ORCHESTRATION_MCP_URL}/debug/rag_search",
        json={"query": query, "top_k": top_k},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return []
    chunks = [{"id": r["id"], "text": r["payload"].get("text", "")} for r in results]
    headers = {"X-Reranker-Shared-Secret": RERANKER_SHARED_SECRET} if RERANKER_SHARED_SECRET else {}
    rr = httpx.post(
        f"{RERANKER_URL}/rerank",
        json={"query": query, "chunks": chunks},
        headers=headers,
        timeout=30,
    )
    rr.raise_for_status()
    scores = {row["id"]: row["score"] for row in rr.json()}
    by_id = {r["id"]: r["payload"].get("filename") for r in results}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(score, by_id[cid]) for cid, score in ranked]


def main() -> None:
    golden = json.loads(DEFAULT_GOLDEN_SET.read_text())
    token = get_token(EVAL_PERSONA)

    worst_answerable = float("inf")
    best_abstention_wrong = float("-inf")

    for case in golden:
        top_k = case.get("top_k", 5)
        ranked = scored_candidates(case["query"], top_k, token)
        expected_abstention = bool(case.get("expected_abstention", False))
        tag = "ABSTENTION" if expected_abstention else "answerable"
        print(f"\n=== {case['id']} [{tag}] top_k={top_k} ===")
        for score, filename in ranked[:4]:
            print(f"  {score:+7.3f}  {filename}")

        if expected_abstention:
            if ranked:
                best_abstention_wrong = max(best_abstention_wrong, ranked[0][0])
        else:
            expect = set(case.get("expect", []))
            target_scores = [score for score, filename in ranked if filename in expect]
            if target_scores:
                worst_answerable = min(worst_answerable, max(target_scores))

    print("\n=== summary ===")
    print(f"worst (still-required) answerable target score: {worst_answerable:+.3f}")
    print(f"best abstention wrong-candidate score:           {best_abstention_wrong:+.3f}")
    if best_abstention_wrong < worst_answerable:
        print(
            f"valid floor interval: ({best_abstention_wrong:.3f}, {worst_answerable:.3f}] -- "
            "any value in this range preserves required recall and suppresses both "
            "abstention probes"
        )
    else:
        print(
            "NO valid floor exists on this corpus: the hardest answerable query scores "
            "at or below the best wrong abstention candidate, so no single threshold "
            "can separate them without either breaking recall or leaving an abstention "
            "case unsuppressed. Re-run against a freshly seeded corpus if this is "
            "unexpected (see module docstring)."
        )


if __name__ == "__main__":
    main()
