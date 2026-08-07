"""Issue #514 item 4 / NFR-4: unit coverage for the latency benchmark's pure
aggregation logic -- Prometheus histogram parsing/deltas/quantiles and the
client-side percentile math. The load driver itself needs a live stack and is
exercised by the e2e benchmark step."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

import pytest

# scripts/ ships no package; put it on the path the same way the harness runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from benchmark_latency import (
    delta_histograms,
    histogram_quantile,
    parse_stage_histograms,
    percentile,
    stage_consistency_warnings,
    summarize_samples,
    summarize_stage,
)

# Label order matches what prometheus_client actually emits: alphabetical,
# i.e. le BEFORE stage. The first CI artifact proved why this matters -- a
# stage-first regex matched no bucket line, and every percentile estimated 0
# while the sum/count-derived means stayed correct.
EXPOSITION = """\
# HELP nexus_rag_query_stage_seconds Wall-clock duration of one stage of a rag_search call.
# TYPE nexus_rag_query_stage_seconds histogram
nexus_rag_query_stage_seconds_bucket{le="0.01",stage="embed"} 0.0
nexus_rag_query_stage_seconds_bucket{le="0.05",stage="embed"} 6.0
nexus_rag_query_stage_seconds_bucket{le="0.1",stage="embed"} 10.0
nexus_rag_query_stage_seconds_bucket{le="+Inf",stage="embed"} 10.0
nexus_rag_query_stage_seconds_sum{stage="embed"} 0.55
nexus_rag_query_stage_seconds_count{stage="embed"} 10.0
nexus_rag_query_stage_seconds_bucket{le="0.01",stage="rerank"} 2.0
nexus_rag_query_stage_seconds_bucket{le="+Inf",stage="rerank"} 2.0
nexus_rag_query_stage_seconds_sum{stage="rerank"} 0.004
nexus_rag_query_stage_seconds_count{stage="rerank"} 2.0
nexus_rag_queries_total{outcome="ok"} 10.0
"""


class TestParsing:
    def test_parses_buckets_sum_and_count_per_stage(self):
        stages = parse_stage_histograms(EXPOSITION)

        assert stages["embed"]["count"] == 10.0
        assert stages["embed"]["sum"] == pytest.approx(0.55)
        assert stages["embed"]["buckets"][0.05] == 6.0
        assert stages["embed"]["buckets"][float("inf")] == 10.0
        assert stages["rerank"]["count"] == 2.0

    def test_label_order_is_irrelevant(self):
        # Robustness against producers that emit stage before le.
        swapped = EXPOSITION.replace('le="0.05",stage="embed"', 'stage="embed",le="0.05"')

        assert parse_stage_histograms(swapped)["embed"]["buckets"][0.05] == 6.0

    def test_real_prometheus_client_output_round_trips(self):
        # The regression the first CI run hit, pinned against the real
        # library rather than a hand-written fixture.
        prometheus_client = pytest.importorskip("prometheus_client")

        registry = prometheus_client.CollectorRegistry()
        h = prometheus_client.Histogram(
            "nexus_rag_query_stage_seconds",
            "d",
            ["stage"],
            buckets=(0.01, 0.05),
            registry=registry,
        )
        h.labels(stage="embed").observe(0.02)
        text = prometheus_client.generate_latest(registry).decode()

        stages = parse_stage_histograms(text)

        assert stages["embed"]["count"] == 1.0
        assert stages["embed"]["buckets"][0.05] == 1.0
        assert histogram_quantile(stages["embed"], 0.5) is not None

    def test_unrelated_metrics_are_ignored(self):
        assert "ok" not in parse_stage_histograms(EXPOSITION)


class TestDeltas:
    def test_delta_isolates_one_load_levels_requests(self):
        before = parse_stage_histograms(EXPOSITION)
        after = parse_stage_histograms(
            EXPOSITION.replace("} 10.0", "} 15.0").replace("0.55", "0.85").replace("6.0", "8.0")
        )

        delta = delta_histograms(before, after)["embed"]

        assert delta["count"] == 5.0
        assert delta["sum"] == pytest.approx(0.30)
        assert delta["buckets"][0.05] == 2.0

    def test_stage_absent_before_deltas_from_zero(self):
        after = parse_stage_histograms(EXPOSITION)

        delta = delta_histograms({}, after)["embed"]

        assert delta["count"] == 10.0


class TestHistogramQuantile:
    DELTA: ClassVar[dict] = {
        "buckets": {0.01: 0.0, 0.05: 6.0, 0.1: 10.0, float("inf"): 10.0},
        "sum": 0.55,
        "count": 10.0,
    }

    def test_median_interpolates_inside_its_bucket(self):
        # target rank 5 of 10 falls in the (0.01, 0.05] bucket holding 6
        # observations: 0.01 + (5/6) * 0.04.
        assert histogram_quantile(self.DELTA, 0.50) == pytest.approx(0.01 + (5 / 6) * 0.04)

    def test_p95_lands_in_the_last_finite_bucket(self):
        q = histogram_quantile(self.DELTA, 0.95)

        assert 0.05 < q <= 0.1

    def test_empty_delta_is_none(self):
        assert histogram_quantile({"buckets": {}, "sum": 0.0, "count": 0.0}, 0.5) is None

    def test_inf_bucket_returns_last_finite_edge_rather_than_inf(self):
        delta = {"buckets": {0.01: 0.0, float("inf"): 4.0}, "sum": 9.0, "count": 4.0}

        assert histogram_quantile(delta, 0.99) == 0.01


class TestClientPercentiles:
    def test_nearest_rank_percentiles(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]

        assert percentile(samples, 0.50) == 50.0
        assert percentile(samples, 0.95) == 100.0
        assert percentile(samples, 0.01) == 10.0

    def test_empty_is_none(self):
        assert percentile([], 0.5) is None

    def test_summaries_carry_n_and_means(self):
        s = summarize_samples([10.0, 30.0])

        assert s == {"n": 2, "p50_ms": 10.0, "p95_ms": 30.0, "mean_ms": 20.0}

    def test_stage_summary_marks_estimates_and_exact_mean(self):
        s = summarize_stage(TestHistogramQuantile.DELTA)

        assert s["n"] == 10
        assert s["mean_ms"] == 55  # exact from sum/count
        assert s["p50_ms_est"] == round((0.01 + (5 / 6) * 0.04) * 1000)


class TestConsistencyWarnings:
    """Issue #536: the label-order parser bug produced 0 ms percentiles next
    to a 96 ms mean and nothing complained. A stage whose p95 estimate is
    arithmetically impossible against its own exact mean must be flagged."""

    def test_impossible_p95_is_flagged(self):
        stages = {"embed": {"n": 55, "p50_ms_est": 0, "p95_ms_est": 0, "mean_ms": 96}}

        assert stage_consistency_warnings(stages) == ["embed"]

    def test_missing_p95_with_a_real_mean_is_flagged(self):
        stages = {"rerank": {"n": 5, "p50_ms_est": None, "p95_ms_est": None, "mean_ms": 52}}

        assert stage_consistency_warnings(stages) == ["rerank"]

    def test_consistent_stages_are_not_flagged(self):
        stages = {
            "embed": {"n": 55, "p50_ms_est": 90, "p95_ms_est": 140, "mean_ms": 96},
            "retrieve": {"n": 55, "p50_ms_est": 50, "p95_ms_est": 80, "mean_ms": 57},
        }

        assert stage_consistency_warnings(stages) == []

    def test_zero_or_absent_mean_never_flags(self):
        # A stage that truly saw sub-millisecond work rounds its mean to 0;
        # that is not evidence of a broken parser.
        stages = {"fast": {"n": 2, "p50_ms_est": 0, "p95_ms_est": 0, "mean_ms": 0}}

        assert stage_consistency_warnings(stages) == []
