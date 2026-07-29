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

import evaluate_retrieval
from evaluate_retrieval import (
    compare_to_baseline,
    evaluate,
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


def _result(filename: str, status: str = "approved", document_id: str = "id-0") -> dict:
    return {"filename": filename, "document_id": document_id, "status": status}


class TestLeakDetectionIsIdentityNotFilename:
    """Issue #226: two documents can share a filename in different states, so
    the leak check must key off each returned chunk's own `status` -- which
    the access filter guarantees is "approved" for anything retrievable at
    all -- rather than matching golden_queries.json's `forbid` filenames
    against the filenames of whatever came back.
    """

    def test_duplicate_filename_in_a_different_state_is_not_a_leak(self, monkeypatch):
        # An approved document shares a filename with an unrelated
        # pending_review document that golden_queries.json lists as
        # forbidden. Only the approved copy is ever returned by the real
        # filter, so this must not be reported as a leak.
        returned = [_result("draft-travel-policy.md", status="approved", document_id="approved-id")]
        monkeypatch.setattr(evaluate_retrieval, "run_query", lambda *a, **k: returned)
        golden_set = [
            {
                "query": "TDY travel reimbursement procedures",
                "expect": [],
                "forbid": ["draft-travel-policy.md"],
                "top_k": 5,
            }
        ]

        report = evaluate(golden_set, token="t", persona="dave-admin")

        assert report["total_forbidden_leaks"] == 0
        q = report["queries"][0]
        assert q["unapproved_leaks"] == []
        # Still surfaced for human diagnosis, just not fatal.
        assert q["content_overlap"] == ["draft-travel-policy.md"]

    def test_unapproved_status_is_a_hard_leak_even_without_a_filename_match(self, monkeypatch):
        # A result whose status isn't "approved" is a genuine FR-26 defect
        # (the access filter itself failed) regardless of whether its
        # filename happens to appear in this query's `forbid` list.
        returned = [_result("some-other-doc.md", status="pending_review", document_id="leaked-id")]
        monkeypatch.setattr(evaluate_retrieval, "run_query", lambda *a, **k: returned)
        golden_set = [{"query": "anything", "expect": [], "forbid": [], "top_k": 5}]

        report = evaluate(golden_set, token="t", persona="dave-admin")

        assert report["total_forbidden_leaks"] == 1
        assert report["queries"][0]["unapproved_leaks"] == returned
