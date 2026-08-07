"""FR-30/FR-32: unit coverage for the retrieval-eval trend store and the
baseline-regression comparison. The scoring itself (`evaluate`) needs a live
orchestration-mcp and is exercised by the golden-query e2e job; these tests
cover the pure history/baseline logic that decides pass vs. regression."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

# scripts/ ships no package; put it on the path the same way the harness runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evaluate_retrieval
from evaluate_retrieval import (
    compare_to_baseline,
    config_fingerprint,
    evaluate,
    latest_prior_report,
    persist_report,
    rank_metrics,
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

        report = evaluate(golden_set, token_for=lambda p: "t", persona="dave-admin")

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

        report = evaluate(golden_set, token_for=lambda p: "t", persona="dave-admin")

        assert report["total_forbidden_leaks"] == 1
        assert report["queries"][0]["unapproved_leaks"] == returned


class TestRecallMisses:
    """Issue #397: a recall miss must be able to fail the run -- e2e.yml always
    documented that contract, but nothing computed it before."""

    @staticmethod
    def _query(query: str, recall: float | None) -> dict:
        return {"query": query, "recall_at_k": recall}

    def test_full_recall_everywhere_is_no_miss(self):
        report = {"queries": [self._query("a", 1.0), self._query("b", 1.0)]}

        assert evaluate_retrieval.recall_misses(report) == []

    def test_partial_recall_is_a_miss(self):
        # Multi-expect query where only some expected documents returned:
        # "any recall miss" means every expected document, not at-least-one.
        report = {"queries": [self._query("a", 0.5)]}

        assert evaluate_retrieval.recall_misses(report) == ["a"]

    def test_zero_recall_is_a_miss(self):
        report = {"queries": [self._query("a", 1.0), self._query("b", 0.0)]}

        assert evaluate_retrieval.recall_misses(report) == ["b"]

    def test_abstention_queries_are_never_misses(self):
        # Empty `expect` -> recall None: their contract is the FR-26 leak
        # check, not recall.
        report = {"queries": [self._query("abstain", None)]}

        assert evaluate_retrieval.recall_misses(report) == []


class TestRankMetrics:
    """Issue #514: recall is blind to ordering; these metrics are not. The
    load-bearing property is that the same set of returned documents scores
    differently depending on where the relevant one sits."""

    def test_rank_one_beats_rank_five_on_the_same_returned_set(self):
        at_1 = rank_metrics(["hit.md", "a.md", "b.md", "c.md", "d.md"], ["hit.md"], top_k=5)
        at_5 = rank_metrics(["a.md", "b.md", "c.md", "d.md", "hit.md"], ["hit.md"], top_k=5)

        assert at_1["reciprocal_rank"] == 1.0
        assert at_5["reciprocal_rank"] == pytest.approx(0.2)
        assert at_1["ndcg_at_k"] == 1.0
        assert at_5["ndcg_at_k"] < at_1["ndcg_at_k"]
        assert at_1["precision_at"]["1"] == 1.0
        assert at_5["precision_at"]["1"] == 0.0

    def test_total_miss_scores_zero_not_none(self):
        m = rank_metrics(["a.md", "b.md"], ["hit.md"], top_k=5)

        assert m["reciprocal_rank"] == 0.0
        assert m["ndcg_at_k"] == 0.0
        assert m["precision_at"] == {"1": 0.0, "3": 0.0, "5": 0.0}

    def test_abstention_scores_none_throughout(self):
        # Same contract as recall: empty `expect` means the FR-26 leak check
        # is the assertion, not ranking.
        m = rank_metrics(["a.md"], [], top_k=5)

        assert m["reciprocal_rank"] is None
        assert m["ndcg_at_k"] is None
        assert m["precision_at"] == {}

    def test_precision_cutoffs_beyond_top_k_are_omitted(self):
        # The paraphrase queries run at top_k=2; precision@3/@5 there would be
        # arithmetically incapable of reaching 1.0.
        m = rank_metrics(["hit.md", "a.md"], ["hit.md"], top_k=2)

        assert m["precision_at"] == {"1": 1.0}

    def test_perfect_ordering_of_multiple_expected_docs_is_ndcg_one(self):
        m = rank_metrics(["x.md", "y.md", "a.md"], ["x.md", "y.md"], top_k=5)

        assert m["ndcg_at_k"] == pytest.approx(1.0)

    def test_duplicate_filename_earns_credit_once(self):
        # Filenames are not unique (issue #226); a duplicated relevant
        # filename must not inflate DCG above the ideal.
        m = rank_metrics(["hit.md", "hit.md", "hit.md"], ["hit.md"], top_k=5)

        assert m["ndcg_at_k"] <= 1.0
        assert m["precision_at"]["3"] == pytest.approx(1 / 3)

    def test_short_returned_list_penalizes_fixed_cutoffs(self):
        # An empty tail holds no relevant document: precision@3 divides by 3
        # even when only one result came back.
        m = rank_metrics(["hit.md"], ["hit.md"], top_k=5)

        assert m["precision_at"]["1"] == 1.0
        assert m["precision_at"]["3"] == pytest.approx(1 / 3)


class TestConfigFingerprint:
    """Issue #514: a persisted report must record the configuration its
    numbers were measured under, so cross-config comparisons are visible."""

    GOLDEN: ClassVar[list[dict]] = [{"query": "q", "expect": ["a.md"], "top_k": 5}]

    def test_same_inputs_same_fingerprint(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed-text")
        a = config_fingerprint(self.GOLDEN, "dave-admin")
        b = config_fingerprint(self.GOLDEN, "dave-admin")

        assert a["fingerprint"] == b["fingerprint"]

    def test_model_change_changes_fingerprint(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed-text")
        before = config_fingerprint(self.GOLDEN, "dave-admin")
        monkeypatch.setenv("EMBEDDING_MODEL", "mxbai-embed-large")
        after = config_fingerprint(self.GOLDEN, "dave-admin")

        assert before["fingerprint"] != after["fingerprint"]
        assert after["config"]["EMBEDDING_MODEL"] == "mxbai-embed-large"

    def test_golden_set_change_changes_fingerprint(self):
        before = config_fingerprint(self.GOLDEN, "dave-admin")
        after = config_fingerprint([*self.GOLDEN, {"query": "new"}], "dave-admin")

        assert before["fingerprint"] != after["fingerprint"]

    def test_unset_and_empty_env_are_recorded_identically(self, monkeypatch):
        # Compose passes "" for unset optional knobs; both mean "default".
        monkeypatch.delenv("RERANK_SCORE_FLOOR", raising=False)
        unset = config_fingerprint(self.GOLDEN, "dave-admin")
        monkeypatch.setenv("RERANK_SCORE_FLOOR", "")
        empty = config_fingerprint(self.GOLDEN, "dave-admin")

        assert unset["fingerprint"] == empty["fingerprint"]
        assert unset["config"]["RERANK_SCORE_FLOOR"] is None


class TestAdvisoryMetricsAndConfigMismatch:
    """Issue #514: rank-aware metrics are reported in comparisons but cannot
    fail a run until promoted; differing config fingerprints annotate the
    comparison instead of silently blending a config change into "drift"."""

    @staticmethod
    def _report(ts: str, mrr: float | None = None, fingerprint: str | None = None) -> dict:
        report = {
            "timestamp": ts,
            "mean_recall_at_k": 0.9,
            "mean_precision_at_k": 0.8,
            "mean_reciprocal_rank": mrr,
            "mean_ndcg_at_k": None,
        }
        if fingerprint is not None:
            report["fingerprint"] = fingerprint
        return report

    def test_advisory_drop_is_reported_but_never_regresses_the_run(self):
        cur = self._report("2026-08-06T00:00:00+00:00", mrr=0.5)
        base = self._report("2026-08-05T00:00:00+00:00", mrr=0.9)
        result = compare_to_baseline(cur, base)

        assert result["metrics"]["mean_reciprocal_rank"]["regressed"] is True
        assert result["metrics"]["mean_reciprocal_rank"]["advisory"] is True
        assert result["regressed"] is False

    def test_baseline_predating_rank_metrics_is_not_comparable_not_fatal(self):
        cur = self._report("2026-08-06T00:00:00+00:00", mrr=0.9)
        base = {
            "timestamp": "2026-08-05T00:00:00+00:00",
            "mean_recall_at_k": 0.9,
            "mean_precision_at_k": 0.8,
        }
        result = compare_to_baseline(cur, base)

        assert result["metrics"]["mean_reciprocal_rank"]["delta"] is None
        assert result["regressed"] is False

    def test_differing_fingerprints_flag_config_mismatch(self):
        cur = self._report("2026-08-06T00:00:00+00:00", fingerprint="aaa")
        base = self._report("2026-08-05T00:00:00+00:00", fingerprint="bbb")

        assert compare_to_baseline(cur, base)["config_mismatch"] is True

    def test_missing_fingerprint_on_either_side_is_not_a_mismatch(self):
        cur = self._report("2026-08-06T00:00:00+00:00", fingerprint="aaa")
        base = self._report("2026-08-05T00:00:00+00:00")

        assert compare_to_baseline(cur, base)["config_mismatch"] is False

    def test_evaluate_report_carries_rank_metrics_and_fingerprint(self, monkeypatch):
        returned = [
            {"filename": "a.md", "document_id": "id-a", "status": "approved"},
            {"filename": "hit.md", "document_id": "id-h", "status": "approved"},
        ]
        monkeypatch.setattr(evaluate_retrieval, "run_query", lambda *a, **k: returned)
        golden_set = [{"query": "q", "expect": ["hit.md"], "top_k": 5}]

        report = evaluate(golden_set, token_for=lambda p: "t", persona="dave-admin")

        assert report["mean_reciprocal_rank"] == pytest.approx(0.5)
        assert 0 < report["mean_ndcg_at_k"] < 1
        assert report["fingerprint"]
        assert report["config"]["golden_set_sha256"]
        assert report["queries"][0]["precision_at"]["5"] == pytest.approx(0.2)


class TestPersonaCases:
    """Issue #514: a golden case may name its own querying persona, so recall
    and the FR-26 leak check run under multiple claims sets. Tokens must be
    fetched for the case's persona, not the run default."""

    def test_case_persona_overrides_the_default_and_gets_its_own_token(self, monkeypatch):
        used_tokens = []

        def fake_run_query(token, query, top_k):
            used_tokens.append(token)
            return []

        monkeypatch.setattr(evaluate_retrieval, "run_query", fake_run_query)
        golden_set = [
            {"query": "a", "expect": [], "top_k": 5},
            {"query": "b", "expect": [], "top_k": 5, "persona": "bob-query"},
        ]

        report = evaluate(golden_set, token_for=lambda p: f"token-{p}", persona="dave-admin")

        assert used_tokens == ["token-dave-admin", "token-bob-query"]
        assert [q["persona"] for q in report["queries"]] == ["dave-admin", "bob-query"]

    def test_leak_check_runs_under_the_case_persona(self, monkeypatch):
        # A scope-filter failure surfacing an unapproved chunk to a
        # non-default persona must still be a hard FR-26 leak.
        returned = [{"filename": "x.md", "document_id": "id-x", "status": "pending_review"}]
        monkeypatch.setattr(evaluate_retrieval, "run_query", lambda *a, **k: returned)
        golden_set = [{"query": "q", "expect": [], "persona": "bob-query", "top_k": 5}]

        report = evaluate(golden_set, token_for=lambda p: "t", persona="dave-admin")

        assert report["total_forbidden_leaks"] == 1
        assert report["queries"][0]["persona"] == "bob-query"


class TestAbstentionNoise:
    """Issue #514: what comes back when nothing should. Advisory metric over
    the empty-`expect` queries; never part of the baseline regression gate
    (lower is better, which would invert the shared drop-means-regression
    arithmetic)."""

    def test_mean_returned_count_over_abstention_queries_only(self, monkeypatch):
        responses = {
            "on-topic": [
                {"filename": "hit.md", "document_id": "i1", "status": "approved"},
            ],
            "abstain-noisy": [
                {"filename": "a.md", "document_id": "i2", "status": "approved"},
                {"filename": "b.md", "document_id": "i3", "status": "approved"},
            ],
            "abstain-clean": [],
        }
        monkeypatch.setattr(
            evaluate_retrieval, "run_query", lambda token, query, top_k: responses[query]
        )
        golden_set = [
            {"query": "on-topic", "expect": ["hit.md"], "top_k": 5},
            {"query": "abstain-noisy", "expect": [], "top_k": 5},
            {"query": "abstain-clean", "expect": [], "top_k": 5},
        ]

        report = evaluate(golden_set, token_for=lambda p: "t", persona="dave-admin")

        assert report["mean_abstention_noise"] == pytest.approx(1.0)

    def test_no_abstention_queries_reports_none(self, monkeypatch):
        monkeypatch.setattr(evaluate_retrieval, "run_query", lambda *a, **k: [])
        golden_set = [{"query": "q", "expect": ["hit.md"], "top_k": 5}]

        report = evaluate(golden_set, token_for=lambda p: "t", persona="dave-admin")

        assert report["mean_abstention_noise"] is None

    def test_abstention_noise_is_never_baseline_compared(self):
        assert "mean_abstention_noise" not in evaluate_retrieval._COMPARED_METRICS
        assert "mean_abstention_noise" not in evaluate_retrieval._ADVISORY_METRICS


class TestGoldenFilesAreWellFormed:
    """The committed golden files are data the harness trusts; catch a broken
    edit at unit-test speed instead of at the nightly e2e run."""

    GOLDEN_DIR = Path(__file__).resolve().parents[2] / "scripts"

    def _load(self, name: str) -> list[dict]:
        return json.loads((self.GOLDEN_DIR / name).read_text())

    def test_personas_file_personas_are_query_capable_realm_users(self):
        # eve-purge/alice-ingest hold no rag-query role: a case naming them
        # would "pass" its empty expectation via an authorization error.
        allowed = {"bob-query", "carol-curator", "dave-admin"}
        for case in self._load("golden_queries_personas.json"):
            assert case["persona"] in allowed, case["id"]

    def test_every_case_has_query_expect_and_bounded_top_k(self):
        for name in ("golden_queries.json", "golden_queries_personas.json"):
            for case in self._load(name):
                assert case["query"].strip(), case["id"]
                assert isinstance(case["expect"], list), case["id"]
                assert 1 <= case.get("top_k", 5) <= 50, case["id"]

    def test_main_set_cases_carry_reference_answers_for_the_judge(self):
        # evaluate_rag_quality.py hard-fails on a case without one; keep the
        # constraint visible here rather than discovered at judge runtime.
        for case in self._load("golden_queries.json"):
            assert case.get("reference_answer", "").strip(), case["id"]


class TestConfigFingerprintFailClosed:
    """Issue #525: a cross-config baseline comparison must fail closed by default,
    and the override has to be recorded rather than silently applied.

    The distinction that matters: #514/#515 shipped the fingerprint and *annotated*
    a mismatch while still returning a verdict. An annotated regression is still a
    regression as far as CI's exit code is concerned, so the nightly could go red
    for a model swap and read as a quality regression -- or go green comparing two
    different systems. These tests pin the stricter behaviour.
    """

    def _report(self, fingerprint: str, recall: float = 1.0) -> dict:
        return {
            "mean_recall_at_k": recall,
            "mean_precision_at_k": 1.0,
            "fingerprint": fingerprint,
            "timestamp": "2026-08-06T00:00:00Z",
        }

    def test_mismatch_is_flagged_and_override_not_recorded_by_default(self):
        c = evaluate_retrieval.compare_to_baseline(self._report("aaa"), self._report("bbb"))
        assert c["config_mismatch"] is True
        assert c["config_change_allowed"] is False

    def test_override_is_stamped_into_the_comparison(self):
        c = evaluate_retrieval.compare_to_baseline(
            self._report("aaa"), self._report("bbb"), allow_config_change=True
        )
        assert c["config_mismatch"] is True
        assert c["config_change_allowed"] is True

    def test_same_fingerprint_is_not_a_mismatch(self):
        c = evaluate_retrieval.compare_to_baseline(self._report("aaa"), self._report("aaa"))
        assert c["config_mismatch"] is False

    def test_baseline_predating_fingerprints_is_unknown_not_mismatch(self):
        """Graceful handling of existing history (#525 checklist item 3): a report
        written before fingerprints existed must not be treated as a mismatch, or
        wiring this in would invalidate every trend store already on disk."""
        baseline = {"mean_recall_at_k": 1.0, "mean_precision_at_k": 1.0}
        c = evaluate_retrieval.compare_to_baseline(self._report("aaa"), baseline)
        assert c["config_mismatch"] is False

    def test_mismatch_does_not_by_itself_set_regressed(self):
        """The two verdicts stay independent: a config change is a reason to refuse
        the comparison, not evidence that quality dropped."""
        c = evaluate_retrieval.compare_to_baseline(self._report("aaa"), self._report("bbb"))
        assert c["regressed"] is False
