"""FR-13/FR-16/FR-30/FR-32 (issue #309, Phase 4 of #138): unit coverage for
the pure aggregation logic in scripts/calibrate_tagging_advisory.py -- mining
the `tagging_advisory` outcome already embedded in `document.approve`/
`document.reject` audit entries (services/ingestion-api/app/routes/curate.py)
into a per-suggester accept/override tally. The DB fetch itself
(`fetch_decisions`/`fetch_classification_ranks`) needs a live Postgres with
the dedicated audit-reporting role and is not exercised here -- these tests
build the same row/rank shapes those functions would return and feed them
straight to `aggregate`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ ships no package; put it on the path the same way the harness runs
# (same technique as tests/unit/test_evaluate_retrieval.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from calibrate_tagging_advisory import (
    PiiTally,
    PiiVerdictTally,
    Tally,
    aggregate,
    latest_prior_report,
    persist_report,
)

RANKS = {"UNCLASSIFIED": 1, "CUI": 2, "SECRET": 3, "TOP SECRET": 4}


def _decision(action: str, outcome: dict | None) -> dict:
    return {"action": action, "detail": {"tagging_advisory": outcome}, "created_at": None}


class TestTally:
    def test_agreement_rate_is_none_with_nothing_resolved(self) -> None:
        assert Tally().agreement_rate is None

    def test_agreement_rate_excludes_unresolved(self) -> None:
        tally = Tally()
        tally.record(curator_agreed=True)
        tally.record(curator_agreed=False)
        tally.record(curator_agreed=None)
        assert tally.flagged == 3
        assert tally.unresolved == 1
        # Only the two resolved cases count toward the rate.
        assert tally.agreement_rate == pytest.approx(0.5)


class TestAggregateMarkingMismatch:
    def test_curator_raising_to_flagged_level_counts_as_accepted(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": True,
                    "flagged_classification": "SECRET",
                    "flagged_caveats": [],
                    "final_classification": "SECRET",
                    "final_releasability": ["NATO"],
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["marking_mismatch"] == {
            "flagged": 1,
            "accepted": 1,
            "overridden": 0,
            "unresolved": 0,
            "agreement_rate": 1.0,
        }

    def test_curator_keeping_original_tag_counts_as_overridden(self) -> None:
        decisions = [
            _decision(
                "document.reject",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": True,
                    "flagged_classification": "SECRET",
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["marking_mismatch"]["overridden"] == 1
        assert report["marking_mismatch"]["accepted"] == 0
        assert report["marking_mismatch"]["agreement_rate"] == 0.0

    def test_flagged_value_outside_current_vocabulary_is_unresolved_not_scored(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": True,
                    "flagged_classification": "RETIRED_LEVEL",
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["marking_mismatch"]["unresolved"] == 1
        assert report["marking_mismatch"]["agreement_rate"] is None

    def test_not_flagged_document_is_not_counted(self) -> None:
        decisions = [_decision("document.approve", None)]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["decisions_considered"] == 1
        assert report["decisions_with_advisory"] == 0
        assert report["marking_mismatch"]["flagged"] == 0


class TestAggregatePrecedent:
    def test_precedent_flag_scored_independent_of_marking_mismatch(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "precedent_classification": "SECRET",
                    "precedent_similar_count": 5,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["marking_mismatch"]["flagged"] == 0
        assert report["precedent"] == {
            "flagged": 1,
            "accepted": 0,
            "overridden": 1,
            "unresolved": 0,
            "agreement_rate": 0.0,
        }


class TestAggregateReleasabilityCaveats:
    def test_all_flagged_caveats_present_in_final_is_accepted(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": True,
                    "flagged_classification": None,
                    "flagged_caveats": ["NATO", "FVEY"],
                    "final_classification": "CUI",
                    "final_releasability": ["NATO", "FVEY", "NOFORN"],
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["releasability_caveats"]["accepted"] == 1

    def test_missing_a_flagged_caveat_is_overridden(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": True,
                    "flagged_classification": None,
                    "flagged_caveats": ["NATO", "FVEY"],
                    "final_classification": "CUI",
                    "final_releasability": ["NATO"],
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["releasability_caveats"]["overridden"] == 1


class TestAggregateLlmClassification:
    def test_genuine_under_classification_suggestion_is_scored(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "SECRET",
                    "final_releasability": ["NATO"],
                    "llm_suggested_classification": "SECRET",
                    "llm_suggested_doc_type": "briefing slide",
                    "llm_confidence": 0.9,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["llm_classification"]["flagged"] == 1
        assert report["llm_classification"]["accepted"] == 1
        assert report["llm_doc_type_flags"] == 1

    def test_doc_type_only_disagreement_is_not_scored_as_a_classification_flag(self) -> None:
        # The LLM suggester's `disagrees_with_assigned` can be true purely
        # because doc_type differed, with suggested_classification equal to
        # (not higher than) what was assigned -- that must not be counted as
        # an under-classification flag the curator "overrode" by leaving
        # classification unchanged, since there was never a genuine
        # classification disagreement to score.
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "llm_suggested_classification": "CUI",
                    "llm_suggested_doc_type": "memo",
                    "llm_confidence": 0.6,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["llm_classification"]["flagged"] == 0
        assert report["llm_doc_type_flags"] == 1

    def test_missing_assigned_classification_from_a_pre_309_audit_row_is_unresolved(self) -> None:
        # Rows written before issue #309 added `assigned_classification` to
        # the outcome dict lack it entirely -- must degrade to "unresolved",
        # never crash and never silently misclassify as agreement.
        decisions = [
            _decision(
                "document.approve",
                {
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "llm_suggested_classification": "SECRET",
                    "llm_suggested_doc_type": "memo",
                    "llm_confidence": 0.6,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["llm_classification"]["flagged"] == 0


class TestPiiTally:
    def test_acted_on_rate_is_none_with_nothing_resolved(self) -> None:
        assert PiiTally().acted_on_rate is None

    def test_rejected_and_corrected_count_as_acted_on(self) -> None:
        tally = PiiTally()
        tally.record(
            action="document.reject", assigned_classification="CUI", final_classification="CUI"
        )
        tally.record(
            action="document.approve",
            assigned_classification="CUI",
            final_classification="SECRET",
        )
        tally.record(
            action="document.approve", assigned_classification="CUI", final_classification="CUI"
        )
        assert tally.flagged == 3
        assert tally.rejected == 1
        assert tally.approved_corrected == 1
        assert tally.approved_unchanged == 1
        # Two of three resolved cases were acted on (rejected + corrected).
        assert tally.acted_on_rate == pytest.approx(2 / 3)

    def test_missing_classification_is_unresolved_not_scored(self) -> None:
        tally = PiiTally()
        tally.record(
            action="document.approve", assigned_classification=None, final_classification="CUI"
        )
        assert tally.unresolved == 1
        assert tally.acted_on_rate is None


class TestAggregatePii:
    def test_pii_regex_finding_approved_unchanged(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "pii_regex_kinds": ["ssn"],
                    "pii_regex_count": 1,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["pii_regex"] == {
            "flagged": 1,
            "approved_unchanged": 1,
            "approved_corrected": 0,
            "rejected": 0,
            "unresolved": 0,
            "acted_on_rate": 0.0,
        }
        assert report["pii_llm"]["flagged"] == 0

    def test_pii_llm_finding_approved_with_correction_counts_as_acted_on(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "SECRET",
                    "final_releasability": ["NONE"],
                    "pii_llm_kinds": ["spelled-out SSN"],
                    "pii_llm_count": 1,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["pii_llm"]["approved_corrected"] == 1
        assert report["pii_llm"]["acted_on_rate"] == pytest.approx(1.0)

    def test_pii_finding_rejected_counts_as_acted_on(self) -> None:
        decisions = [
            _decision(
                "document.reject",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "pii_regex_kinds": ["credit_card"],
                    "pii_regex_count": 1,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["pii_regex"]["rejected"] == 1
        assert report["pii_regex"]["acted_on_rate"] == pytest.approx(1.0)

    def test_no_pii_finding_is_not_counted(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": True,
                    "flagged_classification": "SECRET",
                    "flagged_caveats": [],
                    "final_classification": "SECRET",
                    "final_releasability": ["NATO"],
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        assert report["pii_regex"]["flagged"] == 0
        assert report["pii_llm"]["flagged"] == 0


class TestPiiVerdictTally:
    """Issue #380: calibration for #378's `llm_verdict` -- record() is
    exercised directly here (unlike PiiTally above, which the
    TestAggregatePii* classes exercise indirectly through aggregate())."""

    def test_agreement_rate_is_none_with_nothing_resolved(self) -> None:
        assert PiiVerdictTally().agreement_rate is None

    def test_all_false_positive_and_curator_left_it_unchanged_is_agreement(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.approve",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=2,
            verified_count=2,
            false_positive_count=2,
        )
        assert tally.predicted_false_positive == 1
        assert tally.predicted_false_positive_agreed == 1
        assert tally.predicted_false_positive_overridden == 0
        assert tally.agreement_rate == pytest.approx(1.0)

    def test_all_false_positive_but_curator_rejected_is_override(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.reject",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=1,
            verified_count=1,
            false_positive_count=1,
        )
        assert tally.predicted_false_positive_overridden == 1
        assert tally.agreement_rate == pytest.approx(0.0)

    def test_all_genuine_and_curator_rejected_is_agreement(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.reject",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=1,
            verified_count=1,
            false_positive_count=0,
        )
        assert tally.predicted_genuine == 1
        assert tally.predicted_genuine_agreed == 1
        assert tally.agreement_rate == pytest.approx(1.0)

    def test_all_genuine_but_curator_approved_unchanged_is_override(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.approve",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=1,
            verified_count=1,
            false_positive_count=0,
        )
        assert tally.predicted_genuine_overridden == 1
        assert tally.agreement_rate == pytest.approx(0.0)

    def test_mixed_verdicts_across_findings_is_skipped(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.approve",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=2,
            verified_count=2,
            false_positive_count=1,
        )
        assert tally.skipped == 1
        assert tally.predicted_false_positive == 0
        assert tally.predicted_genuine == 0

    def test_partial_verification_is_skipped(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.approve",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=3,
            verified_count=2,
            false_positive_count=2,
        )
        assert tally.skipped == 1

    def test_zero_regex_count_is_skipped(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.approve",
            assigned_classification="CUI",
            final_classification="CUI",
            regex_count=0,
            verified_count=0,
            false_positive_count=0,
        )
        assert tally.skipped == 1

    def test_missing_classification_on_approve_is_unresolved(self) -> None:
        tally = PiiVerdictTally()
        tally.record(
            action="document.approve",
            assigned_classification=None,
            final_classification="CUI",
            regex_count=1,
            verified_count=1,
            false_positive_count=1,
        )
        assert tally.unresolved == 1
        assert tally.predicted_false_positive == 0


class TestAggregatePiiVerdict:
    def test_all_false_positive_approved_unchanged_scored_through_aggregate(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "pii_regex_kinds": ["credit_card", "bank_routing"],
                    "pii_regex_count": 2,
                    "pii_regex_llm_verified_count": 2,
                    "pii_regex_llm_likely_false_positive_count": 2,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        verdict = report["pii_regex_llm_verdict"]
        assert verdict["predicted_false_positive"] == 1
        assert verdict["predicted_false_positive_agreed"] == 1
        assert verdict["agreement_rate"] == pytest.approx(1.0)
        # Base pii_regex tally is still scored independently of the verdict tally.
        assert report["pii_regex"]["approved_unchanged"] == 1

    def test_document_with_no_verification_field_is_not_counted(self) -> None:
        decisions = [
            _decision(
                "document.approve",
                {
                    "assigned_classification": "CUI",
                    "marking_mismatch_flagged": False,
                    "flagged_classification": None,
                    "flagged_caveats": [],
                    "final_classification": "CUI",
                    "final_releasability": ["NONE"],
                    "pii_regex_kinds": ["ssn"],
                    "pii_regex_count": 1,
                },
            )
        ]
        report = aggregate(decisions, RANKS).to_dict()
        verdict = report["pii_regex_llm_verdict"]
        assert verdict["predicted_false_positive"] == 0
        assert verdict["predicted_genuine"] == 0
        assert verdict["skipped"] == 0


class TestHistoryStore:
    def test_persist_and_find_latest(self, tmp_path: Path) -> None:
        older = {"generated_at": "2026-07-01T00:00:00+00:00"}
        newer = {"generated_at": "2026-07-15T00:00:00+00:00"}
        persist_report(older, tmp_path)
        newest_path = persist_report(newer, tmp_path)

        found = latest_prior_report(tmp_path, exclude=newest_path)
        assert found is not None
        assert "20260701" in found.name

    def test_no_prior_reports_returns_none(self, tmp_path: Path) -> None:
        assert latest_prior_report(tmp_path) is None
