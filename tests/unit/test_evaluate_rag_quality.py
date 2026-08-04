"""Issue #74: pure scoring and baseline guards for the local-judge evaluator.

The live LibreChat/Ollama path is intentionally manual; these tests pin the
parts that must not depend on a particular model response or running stack.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evaluate_rag_quality as qca
from evaluate_rag_quality import (
    JUDGE_PROMPT_VERSION,
    JudgeError,
    average_precision,
    citation_validity,
    compare_to_baseline,
    extract_generation,
    score_judgment,
)


def _context(
    rank: int,
    filename: str = "password-policy.md",
    classification: str = "CUI",
) -> dict:
    return {
        "rank": rank,
        "filename": filename,
        "classification": classification,
        "text": "reference text",
    }


def _judgment(*, relevance: list[bool] | None = None) -> dict:
    relevance = relevance if relevance is not None else [True, False, True]
    return {
        "context_relevance": [
            {"index": index, "score": 0.9 if relevant else 0.1, "relevant": relevant}
            for index, relevant in enumerate(relevance, start=1)
        ],
        "reference_claims": [
            {"claim": "rotation is every 90 days", "supported": True},
            {"claim": "last 12 cannot be reused", "supported": False},
        ],
        "answer_claims": [
            {"claim": "rotation is every 90 days", "supported": True},
            {"claim": "symbols are mandatory", "supported": False},
        ],
        "answer_relevancy": 0.8,
        "answer_correctness": 0.75,
        "abstention_correct": None,
    }


class TestContextualPrecision:
    def test_rewards_relevant_contexts_earlier_in_the_ranking(self) -> None:
        early = average_precision([True, False, True])
        late = average_precision([False, True, True])

        assert early == pytest.approx((1.0 + 2 / 3) / 2)
        assert late == pytest.approx((1 / 2 + 2 / 3) / 2)
        assert early > late

    def test_no_context_is_not_scored_and_all_irrelevant_is_zero(self) -> None:
        assert average_precision([]) is None
        assert average_precision([False, False]) == 0.0


class TestCitationValidity:
    def test_only_exact_retrieved_source_and_classification_pairs_are_valid(self) -> None:
        contexts = [_context(1), _context(2, "network-sop.md", "SECRET")]
        answer = (
            "Rotate every 90 days [password-policy.md, CUI]. Use a token [network-sop.md, CUI]."
        )

        result = citation_validity(answer, contexts)

        assert result["score"] == 0.5
        assert result["citation_count"] == 2
        assert result["valid_citation_count"] == 1
        assert result["cited_sources"] == ["network-sop.md", "password-policy.md"]

    def test_missing_citation_scores_zero_when_context_was_available(self) -> None:
        assert citation_validity("Rotate every 90 days.", [_context(1)])["score"] == 0.0

    def test_citation_is_not_applicable_when_no_context_was_returned(self) -> None:
        assert citation_validity("No approved document was found.", [])["score"] is None


class TestGenerationExtraction:
    def test_requires_a_rag_search_result_not_merely_any_tool_call(self) -> None:
        unrelated = {
            "content": [
                {"type": "tool_call", "tool_call": {"output": "weather is sunny"}},
                {"type": "text", "text": "An answer"},
            ]
        }
        rag_search = {
            "content": [
                {
                    "type": "tool_call",
                    "tool_call": {
                        "output": "Retrieved approved reference passages follow.\nReference 1"
                    },
                },
                {"type": "text", "text": "An answer"},
            ]
        }

        assert extract_generation(unrelated)["rag_search_called"] is False
        assert extract_generation(rag_search)["rag_search_called"] is True


class TestJudgmentScoring:
    def test_derives_all_metrics_without_persisting_claim_text(self) -> None:
        contexts = [_context(1), _context(2), _context(3)]

        result = score_judgment(
            _judgment(),
            contexts,
            "Rotate every 90 days [password-policy.md, CUI].",
            expected_abstention=False,
        )

        assert result["contextual_relevancy"] == pytest.approx((0.9 + 0.1 + 0.9) / 3)
        assert result["contextual_recall"] == 0.5
        assert result["contextual_precision"] == pytest.approx((1.0 + 2 / 3) / 2)
        assert result["faithfulness"] == 0.5
        assert result["answer_relevancy"] == 0.8
        assert result["answer_correctness"] == 0.75
        assert result["citation_validity"] == 1.0
        assert result["evidence"]["supported_answer_claims"] == 1
        assert "cited_sources" not in result["evidence"]
        assert "rotation is every 90 days" not in str(result["evidence"])

    def test_abstention_does_not_get_fake_context_recall_or_faithfulness_scores(self) -> None:
        judgment = _judgment(relevance=[])
        judgment["abstention_correct"] = True

        result = score_judgment(
            judgment,
            [],
            "No approved document was found.",
            expected_abstention=True,
        )

        assert result["contextual_recall"] is None
        assert result["faithfulness"] is None
        assert result["abstention_correct"] is True
        assert result["abstention_undetermined"] is False

    def test_failed_abstention_scores_false_rather_than_erroring(self) -> None:
        """The judge saying "expected to abstain, and it didn't" is a real
        result the schema must accept -- the qca-v1 prompt never told the model
        to emit it, which is how the null case below went unnoticed."""
        judgment = _judgment(relevance=[True])
        judgment["abstention_correct"] = False

        result = score_judgment(
            judgment,
            [_context(1)],
            "The retention period is 90 days.",
            expected_abstention=True,
        )

        assert result["abstention_correct"] is False
        assert result["abstention_undetermined"] is False

    def test_undetermined_abstention_does_not_fail_the_case(self) -> None:
        """Regression for the reviewer's finding on #383: qwen2.5:3b-instruct
        returns null here 100% of the time even when it judges the abstention
        valid on every other field. Hard-erroring turned that into a reported
        generation regression that had not happened.
        """
        judgment = _judgment(relevance=[])
        judgment["abstention_correct"] = None

        result = score_judgment(
            judgment,
            [],
            "No approved document was found.",
            expected_abstention=True,
        )

        assert result["abstention_correct"] is None
        assert result["abstention_undetermined"] is True

    def test_undetermined_abstention_is_not_coerced_to_a_failed_abstention(self) -> None:
        """None and False must stay distinguishable. Coercing an unsure judge to
        False would assert "the model was told to abstain and did not", which in
        the observed case is simply untrue -- trading a loud wrong failure for a
        quiet wrong metric."""
        judgment = _judgment(relevance=[])
        judgment["abstention_correct"] = None

        result = score_judgment(judgment, [], "No approved document.", expected_abstention=True)

        assert result["abstention_correct"] is not False

    def test_rejects_non_boolean_abstention_verdict(self) -> None:
        """Softening null must not soften garbage: a string is still a
        malformed response, not an unsure one."""
        judgment = _judgment(relevance=[])
        judgment["abstention_correct"] = "yes"

        with pytest.raises(JudgeError, match="abstention_correct must be boolean"):
            score_judgment(judgment, [], "No approved document.", expected_abstention=True)

    def test_rejects_out_of_range_scores(self) -> None:
        judgment = _judgment(relevance=[True])
        judgment["answer_relevancy"] = 5

        with pytest.raises(JudgeError, match="between 0 and 1"):
            score_judgment(
                judgment,
                [_context(1)],
                "answer",
                expected_abstention=False,
            )

    def test_rejects_missing_context_judgments(self) -> None:
        with pytest.raises(JudgeError, match="one item per retrieved context"):
            score_judgment(
                _judgment(relevance=[True]),
                [_context(1), _context(2)],
                "answer",
                expected_abstention=False,
            )


def _report(
    *,
    judge_model: str = "qwen2.5:3b-instruct",
    prompt_version: str = JUDGE_PROMPT_VERSION,
    value: float = 0.8,
) -> dict:
    report = {
        "timestamp": "2026-08-04T00:00:00+00:00",
        "judge_model": judge_model,
        "judge_prompt_version": prompt_version,
        "golden_set_sha256": "golden-v1",
    }
    for name in (
        "mean_contextual_relevancy",
        "mean_contextual_recall",
        "mean_contextual_precision",
        "mean_faithfulness",
        "mean_answer_relevancy",
        "mean_answer_correctness",
        "mean_citation_validity",
        "abstention_accuracy",
    ):
        report[name] = value
    return report


def test_default_case_report_omits_corpus_content_and_source_names(monkeypatch) -> None:
    monkeypatch.setattr(
        qca,
        "run_retrieval",
        lambda token, query, top_k: [_context(1)],
    )
    monkeypatch.setattr(
        qca,
        "send_and_wait",
        lambda agent_id, user, query: {
            "content": [
                {
                    "type": "tool_call",
                    "tool_call": {
                        "output": "Retrieved approved reference passages follow.\nReference 1"
                    },
                },
                {
                    "type": "text",
                    "text": "Rotate every 90 days [password-policy.md, CUI].",
                },
            ]
        },
    )

    result = qca.evaluate_case(
        {
            "id": "password-rotation",
            "query": "how often should passwords be rotated",
            "reference_answer": "Every 90 days.",
            "expect": ["password-policy.md"],
            "top_k": 1,
        },
        token="token",
        agent_id="agent",
        user="dave-admin",
        judge=lambda payload: _judgment(relevance=[True]),
        max_context_chars=1000,
        include_content=False,
    )

    assert result["rag_search_called"] is True
    assert result["expected_source_count"] == 1
    assert result["returned_context_count"] == 1
    for sensitive_field in (
        "query",
        "reference_answer",
        "generated_answer",
        "contexts",
        "expected_sources",
        "returned_sources",
    ):
        assert sensitive_field not in result
    assert "password-policy.md" not in str(result)


def test_evaluate_refreshes_mcp_connection_and_token_for_each_case(monkeypatch) -> None:
    connections: list[bool] = []
    tokens: list[str] = []

    def token_provider() -> str:
        token = f"token-{len(tokens) + 1}"
        tokens.append(token)
        return token

    def fake_evaluate_case(case, *, token, **kwargs):
        return {
            "id": case["id"],
            "rag_search_called": True,
            "contextual_relevancy": 1.0,
            "contextual_recall": 1.0,
            "contextual_precision": 1.0,
            "faithfulness": 1.0,
            "answer_relevancy": 1.0,
            "answer_correctness": 1.0,
            "citation_validity": 1.0,
            "abstention_correct": None,
            "token_seen": token,
        }

    monkeypatch.setattr(qca, "evaluate_case", fake_evaluate_case)

    report = qca.evaluate(
        [
            {"id": "case-1", "query": "first", "reference_answer": "one"},
            {"id": "case-2", "query": "second", "reference_answer": "two"},
        ],
        token_provider=token_provider,
        agent_id="agent",
        user="dave-admin",
        judge=lambda payload: payload,
        judge_model="local-judge",
        before_case=lambda: connections.append(True),
    )

    assert len(connections) == 2
    assert tokens == ["token-1", "token-2"]
    assert [case["token_seen"] for case in report["queries"]] == tokens


def test_undetermined_abstentions_are_excluded_from_accuracy_but_counted(monkeypatch) -> None:
    """abstention_accuracy must not silently average over a partial set. With
    one determined pass and one undetermined case, accuracy reads 1.0 -- which
    on its own looks like full coverage, so the count is what stops that being
    misread.
    """
    verdicts = iter([True, None])
    undetermined = iter([False, True])

    def fake_evaluate_case(case, *, token, **kwargs):
        return {
            "id": case["id"],
            "rag_search_called": True,
            "contextual_relevancy": 1.0,
            "contextual_recall": None,
            "contextual_precision": 1.0,
            "faithfulness": None,
            "answer_relevancy": 1.0,
            "answer_correctness": 1.0,
            "citation_validity": 1.0,
            "abstention_correct": next(verdicts),
            "abstention_undetermined": next(undetermined),
        }

    monkeypatch.setattr(qca, "evaluate_case", fake_evaluate_case)

    report = qca.evaluate(
        [
            {"id": "abstain-1", "query": "a", "reference_answer": "", "expect_abstention": True},
            {"id": "abstain-2", "query": "b", "reference_answer": "", "expect_abstention": True},
        ],
        token_provider=lambda: "token",
        agent_id="agent",
        user="dave-admin",
        judge=lambda payload: payload,
        judge_model="qwen2.5:3b-instruct",
    )

    assert report["abstention_accuracy"] == 1.0
    assert report["abstention_undetermined"] == 1
    # The undetermined case is not an error: the run stays valid and every other
    # metric is still usable. This is the whole point of the reviewer's fix.
    assert report["errors"] == []
    assert report["tool_call_failures"] == 0


def test_all_undetermined_abstentions_report_null_accuracy_not_zero(monkeypatch) -> None:
    """A run where nothing could be determined must not read as 0.0, which would
    look like every abstention failed."""

    def fake_evaluate_case(case, *, token, **kwargs):
        return {
            "id": case["id"],
            "rag_search_called": True,
            "contextual_relevancy": 1.0,
            "contextual_recall": None,
            "contextual_precision": 1.0,
            "faithfulness": None,
            "answer_relevancy": 1.0,
            "answer_correctness": 1.0,
            "citation_validity": 1.0,
            "abstention_correct": None,
            "abstention_undetermined": True,
        }

    monkeypatch.setattr(qca, "evaluate_case", fake_evaluate_case)

    report = qca.evaluate(
        [{"id": "abstain-1", "query": "a", "reference_answer": "", "expect_abstention": True}],
        token_provider=lambda: "token",
        agent_id="agent",
        user="dave-admin",
        judge=lambda payload: payload,
        judge_model="qwen2.5:3b-instruct",
    )

    assert report["abstention_accuracy"] is None
    assert report["abstention_undetermined"] == 1
    assert report["errors"] == []


class TestBaselineComparison:
    @pytest.mark.parametrize(
        ("field", "current"),
        [
            ("judge_model", "different-model"),
            # Deliberately not a real version string: pinning this to whatever
            # the current JUDGE_PROMPT_VERSION happens not to be makes the test
            # fail on the next legitimate bump, which is how it broke when
            # qca-v1 became qca-v2.
            ("judge_prompt_version", "some-other-prompt-version"),
            ("golden_set_sha256", "golden-v2"),
        ],
    )
    def test_refuses_cross_judge_or_cross_prompt_comparisons(
        self, field: str, current: str
    ) -> None:
        baseline = _report()
        changed = _report()
        changed[field] = current

        result = compare_to_baseline(changed, baseline, tolerance=0.05)

        assert result["comparable"] is False
        assert result["regressed"] is False
        assert field in result["reason"]

    def test_same_judge_drop_beyond_tolerance_is_a_regression(self) -> None:
        result = compare_to_baseline(_report(value=0.7), _report(value=0.8), tolerance=0.05)

        assert result["comparable"] is True
        assert result["regressed"] is True
        assert result["metrics"]["mean_faithfulness"]["regressed"] is True

    def test_same_judge_drop_within_tolerance_is_allowed(self) -> None:
        result = compare_to_baseline(_report(value=0.76), _report(value=0.8), tolerance=0.05)

        assert result["comparable"] is True
        assert result["regressed"] is False
