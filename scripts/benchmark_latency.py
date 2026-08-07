"""Issue #514 item 4 / NFR-4: per-stage retrieval latency benchmark.

REQUIREMENTS.md leaves NFR-4's end-to-end latency budget as an open question
because no numbers existed to set one from. This produces them: p50/p95/mean
end-to-end latency at one or more concurrency levels, plus the same per stage
(embed, retrieve, rerank), so a budget proposal and a stage-level regression
comparison are both grounded in measurements.

Two sources, deliberately different:

* **End-to-end**: measured client-side around each `/debug/rag_search` call --
  exact percentiles over the individual requests, the same wall-clock any
  caller can already observe.
* **Per-stage**: `_timings_ms` is deliberately never in the response body --
  per-stage latency correlates with how much the access filter matched, which
  is a cleaner membership-inference side channel than total wall time (#127).
  This benchmark honors that decision: stage numbers come from the *operator*
  surface instead -- the `nexus_rag_query_stage_seconds` Prometheus histogram
  (issue #72) on `/metrics`, snapshotted before and after each load level.
  The bucket-delta gives exact means (sum/count) and interpolated p50/p95
  estimates (marked `*_est`: bucket-resolution, not exact order statistics).

Queries come from the golden set (each at its own `top_k`), so the load shape
matches what the quality harness measures and the config fingerprint
(evaluate_retrieval.config_fingerprint) identifies the same configuration in
both reports. Absolute numbers are only meaningful for the hardware that
produced them -- a CI runner's figures inform the *stage split* and *trend*,
not the production NFR-4 budget, which needs a run on representative hardware.

Run inside the compose network (docker compose --profile eval run --rm
benchmark-latency) or host-side with the service URLs exported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx
from _keycloak import get_token
from evaluate_retrieval import (
    DEFAULT_GOLDEN_SET,
    EVAL_PERSONA,
    ORCHESTRATION_MCP_URL,
    config_fingerprint,
)

METRICS_URL = f"{ORCHESTRATION_MCP_URL}/metrics"
STAGE_METRIC = "nexus_rag_query_stage_seconds"

# Label order in the exposition format is not guaranteed -- prometheus_client
# emits alphabetically ({le=...,stage=...}), other producers differ -- so the
# labels are parsed as a dict rather than positionally. (The first CI run's
# artifact had every bucket silently unmatched by a stage-first regex: means
# were exact while every percentile estimated 0.)
_SAMPLE_RE = re.compile(rf"^{STAGE_METRIC}(_bucket|_sum|_count){{([^}}]*)}} ([0-9.eE+-]+|NaN)$")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_stage_histograms(text: str) -> dict[str, dict]:
    """Per-stage cumulative-bucket snapshot from Prometheus exposition text."""
    stages: dict[str, dict] = {}

    def stage(name: str) -> dict:
        return stages.setdefault(name, {"buckets": {}, "sum": 0.0, "count": 0.0})

    for line in text.splitlines():
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        suffix, labels_text, value = m.groups()
        labels = dict(_LABEL_RE.findall(labels_text))
        name = labels.get("stage")
        if name is None:
            continue
        if suffix == "_bucket":
            if "le" not in labels:
                continue
            le = float("inf") if labels["le"] == "+Inf" else float(labels["le"])
            stage(name)["buckets"][le] = float(value)
        elif suffix == "_sum":
            stage(name)["sum"] = float(value)
        else:
            stage(name)["count"] = float(value)
    return stages


def delta_histograms(before: dict[str, dict], after: dict[str, dict]) -> dict[str, dict]:
    """after - before, per stage: the histogram of only this load level's
    requests. Stages absent before (first traffic ever) delta against zero."""
    deltas: dict[str, dict] = {}
    for name, cur in after.items():
        prev = before.get(name, {"buckets": {}, "sum": 0.0, "count": 0.0})
        deltas[name] = {
            "buckets": {
                le: cum - prev["buckets"].get(le, 0.0) for le, cum in cur["buckets"].items()
            },
            "sum": cur["sum"] - prev["sum"],
            "count": cur["count"] - prev["count"],
        }
    return deltas


def histogram_quantile(delta: dict, q: float) -> float | None:
    """Interpolated quantile in seconds from a cumulative-bucket delta, the
    same estimate Prometheus's histogram_quantile computes. None when the
    delta holds no observations. The top bucket has no upper edge; its lower
    edge is returned (an underestimate, flagged by being the last edge)."""
    count = delta["count"]
    if count <= 0:
        return None
    edges = sorted(delta["buckets"])
    target = q * count
    prev_edge, prev_cum = 0.0, 0.0
    for edge in edges:
        cum = delta["buckets"][edge]
        if cum >= target:
            if edge == float("inf"):
                return prev_edge
            span = cum - prev_cum
            fraction = ((target - prev_cum) / span) if span > 0 else 1.0
            return prev_edge + (edge - prev_edge) * fraction
        prev_edge, prev_cum = edge, cum
    return prev_edge


def percentile(values: list[float], q: float) -> float | None:
    """Exact nearest-rank percentile of raw samples; None on empty input."""
    if not values:
        return None
    ordered = sorted(values)
    rank = min(len(ordered), max(1, math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def summarize_samples(samples_ms: list[float]) -> dict:
    return {
        "n": len(samples_ms),
        "p50_ms": percentile(samples_ms, 0.50),
        "p95_ms": percentile(samples_ms, 0.95),
        "mean_ms": (sum(samples_ms) / len(samples_ms)) if samples_ms else None,
    }


def summarize_stage(delta: dict) -> dict:
    count = delta["count"]
    p50 = histogram_quantile(delta, 0.50)
    p95 = histogram_quantile(delta, 0.95)
    return {
        "n": int(count),
        "p50_ms_est": round(p50 * 1000) if p50 is not None else None,
        "p95_ms_est": round(p95 * 1000) if p95 is not None else None,
        "mean_ms": round(delta["sum"] / count * 1000) if count > 0 else None,
    }


def stage_consistency_warnings(stages: dict[str, dict]) -> list[str]:
    """Stages whose percentile estimates are arithmetically impossible against
    their own exact mean (p95 below half the mean, or missing while a mean
    exists). Issue #536: the first CI run's report carried a 0 ms p95 next to
    a 96 ms mean -- a silently label-order-broken bucket parser, visible only
    because the same distribution was derived two ways. This makes that class
    of regression announce itself instead of waiting for a human to notice
    the numbers can't coexist."""
    suspect = []
    for name, s in stages.items():
        mean = s.get("mean_ms")
        p95 = s.get("p95_ms_est")
        if mean is None or mean == 0:
            continue
        if p95 is None or p95 < mean / 2:
            suspect.append(name)
    return sorted(suspect)


async def _one_query(
    client: httpx.AsyncClient, token: str, query: str, top_k: int, sem: asyncio.Semaphore
) -> float:
    async with sem:
        started = perf_counter()
        resp = await client.post(
            f"{ORCHESTRATION_MCP_URL}/debug/rag_search",
            json={"query": query, "top_k": top_k},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        resp.raise_for_status()
        return (perf_counter() - started) * 1000


async def run_level(
    token: str, cases: list[dict], concurrency: int, repetitions: int
) -> list[float]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        tasks = [
            _one_query(client, token, case["query"], case.get("top_k", 5), sem)
            for _ in range(repetitions)
            for case in cases
        ]
        return list(await asyncio.gather(*tasks))


def scrape_metrics() -> dict[str, dict]:
    resp = httpx.get(METRICS_URL, timeout=10)
    resp.raise_for_status()
    return parse_stage_histograms(resp.text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", type=Path, default=DEFAULT_GOLDEN_SET)
    parser.add_argument(
        "--concurrency",
        default="1,4",
        help="comma-separated concurrency levels to measure (default 1,4)",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=5,
        help="passes over the golden set per level (default 5)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="unmeasured passes first, so model/collection cold starts don't "
        "land in the numbers (default 1)",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    cases = json.loads(args.golden_set.read_text())
    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    token = get_token(EVAL_PERSONA)

    if args.warmup:
        asyncio.run(run_level(token, cases, concurrency=2, repetitions=args.warmup))

    report: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "persona": EVAL_PERSONA,
        **config_fingerprint(cases, EVAL_PERSONA),
        "queries_per_pass": len(cases),
        "repetitions": args.repetitions,
        "levels": {},
    }
    for level in levels:
        before = scrape_metrics()
        samples = asyncio.run(run_level(token, cases, level, args.repetitions))
        after = scrape_metrics()
        stages = {
            name: summarize_stage(delta)
            for name, delta in delta_histograms(before, after).items()
            if delta["count"] > 0
        }
        report["levels"][str(level)] = {
            "end_to_end_client": summarize_samples(samples),
            "stages": stages,
            "suspect_stages": stage_consistency_warnings(stages),
        }

    print(
        f"Latency benchmark @ {report['timestamp']} "
        f"(persona={report['persona']}, config={report['fingerprint'][:12]}, "
        f"{report['queries_per_pass']} queries x {report['repetitions']} reps)"
    )
    for level, data in report["levels"].items():
        e2e = data["end_to_end_client"]
        print(
            f"  concurrency {level}: end-to-end p50={e2e['p50_ms']:.0f}ms "
            f"p95={e2e['p95_ms']:.0f}ms mean={e2e['mean_ms']:.0f}ms (n={e2e['n']})"
        )
        for stage in ("embed", "retrieve", "rerank", "total"):
            if stage in data["stages"]:
                s = data["stages"][stage]
                print(
                    f"    {stage:9s} p50~{s['p50_ms_est']}ms p95~{s['p95_ms_est']}ms "
                    f"mean={s['mean_ms']}ms (n={s['n']}, histogram estimate)"
                )
        if data["suspect_stages"]:
            print(
                f"    WARNING: percentile estimates inconsistent with exact means "
                f"for {data['suspect_stages']} -- treat them as broken, see issue #536",
                file=sys.stderr,
            )
    print(f"BENCHMARK_JSON: {json.dumps(report, separators=(',', ':'))}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"Wrote report to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
