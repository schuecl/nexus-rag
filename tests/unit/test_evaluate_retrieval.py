"""FR-30/FR-32: unit coverage for the retrieval-eval trend store and the
baseline-regression comparison. The scoring itself (`evaluate`) needs a live
orchestration-mcp and is exercised by the golden-query e2e job; these tests
cover the pure history/baseline logic that decides pass vs. regression."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ ships no package; put it on the path the same way the harness runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evaluate_retrieval import (
    compare_to_baseline,
    latest_prior_report,
    persist_report,
)


def _report(ts: str, recall: float | None, precision: float | None, leaks: int = 0) -> dict:
    return {
        "timestamp": ts,
        "persona": "dave-admin",
        "mean_recall_at_k": recall,
        "mean_precision_at_k": precision,
        "total_forbidden_leaks": leaks,
        "queries": [],
    }


class TestCompareToBaseline:
    def test_equal_metrics_do_not_regress(self):
        cur = _report("2026-07-02T00:00:00+00:00", 0.9, 0.8)
        base = _report("2026-07-01T00:00:00+00:00", 0.9, 0.8)
        assert compare_to_baseline(cur, base)["regressed"] is False

    def test_improvement_does_not_regress(self):
        cur = _report("2026-07-02T00:00:00+00:00", 1.0, 0.9)
        base = _report("2026-07-01T00:00:00+00:00", 0.9, 0.8)
        result = compare_to_baseline(cur, base)
        assert result["regressed"] is False
        assert result["metrics"]["mean_recall_at_k"]["delta"] == pytest.approx(0.1)

    def test_any_drop_regresses_at_zero_tolerance(self):
        cur = _report("2026-07-02T00:00:00+00:00", 0.89, 0.8)
        base = _report("2026-07-01T00:00:00+00:00", 0.90, 0.8)
        result = compare_to_baseline(cur, base, tolerance=0.0)
        assert result["regressed"] is True
        assert result["metrics"]["mean_recall_at_k"]["regressed"] is True
        assert result["metrics"]["mean_precision_at_k"]["regressed"] is False

    def test_drop_within_tolerance_is_not_a_regression(self):
        cur = _report("2026-07-02T00:00:00+00:00", 0.88, 0.8)
        base = _report("2026-07-01T00:00:00+00:00", 0.90, 0.8)
        assert compare_to_baseline(cur, base, tolerance=0.05)["regressed"] is False

    def test_drop_exactly_at_tolerance_is_not_a_regression(self):
        # delta == -tolerance is allowed; only a drop *greater* than tolerance
        # fails. Uses exact binary fractions so the boundary isn't a
        # floating-point coin toss (0.5 - 0.75 == -0.25 exactly).
        cur = _report("2026-07-02T00:00:00+00:00", 0.5, 0.8)
        base = _report("2026-07-01T00:00:00+00:00", 0.75, 0.8)
        assert compare_to_baseline(cur, base, tolerance=0.25)["regressed"] is False

    def test_none_metric_is_reported_but_never_regresses(self):
        cur = _report("2026-07-02T00:00:00+00:00", None, 0.8)
        base = _report("2026-07-01T00:00:00+00:00", 0.9, 0.8)
        result = compare_to_baseline(cur, base)
        assert result["regressed"] is False
        assert result["metrics"]["mean_recall_at_k"]["delta"] is None


class TestTrendStore:
    def test_persist_writes_a_timestamped_sortable_file(self, tmp_path):
        report = _report("2026-07-01T12:30:00+00:00", 0.9, 0.8)
        path = persist_report(report, tmp_path / "history")
        assert path.exists()
        assert path.name == "retrieval-eval-20260701T123000+0000.json"

    def test_latest_prior_report_picks_the_newest_and_excludes_the_current(self, tmp_path):
        history = tmp_path / "history"
        older = persist_report(_report("2026-07-01T00:00:00+00:00", 0.9, 0.8), history)
        newer = persist_report(_report("2026-07-02T00:00:00+00:00", 0.9, 0.8), history)
        # Excluding the run just written yields the previous run as the baseline.
        assert latest_prior_report(history, exclude=newer) == older
        # With nothing to exclude, the newest overall is returned.
        assert latest_prior_report(history) == newer

    def test_latest_prior_report_is_none_on_empty_history(self, tmp_path):
        assert latest_prior_report(tmp_path / "empty") is None
