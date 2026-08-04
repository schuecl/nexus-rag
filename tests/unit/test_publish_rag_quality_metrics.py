from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import publish_rag_quality_metrics as quality_metrics
from publish_rag_quality_metrics import ReportError, build_exposition, publish


def _report(**overrides):
    report = {
        "timestamp": "2026-08-04T12:30:00+00:00",
        "persona": "dave-admin-sensitive-name",
        "agent_id": "agent-secret-id",
        "golden_set_sha256": "a" * 64,
        "judge_model": "internal-model-name",
        "judge_prompt_version": "qca-v1",
        "query_count": 2,
        "scored_query_count": 2,
        "tool_call_failures": 0,
        "errors": [],
        "mean_contextual_relevancy": 0.81,
        "mean_contextual_recall": 0.82,
        "mean_contextual_precision": 0.83,
        "mean_faithfulness": 0.84,
        "mean_answer_relevancy": 0.85,
        "mean_answer_correctness": 0.86,
        "mean_citation_validity": 0.87,
        "abstention_accuracy": 1.0,
        "queries": [
            {
                "id": "descriptive-password-policy-case",
                "query": "SECRET QUERY TEXT",
                "generated_answer": "SECRET ANSWER TEXT",
                "contexts": [{"text": "SECRET CONTEXT", "filename": "SECRET SOURCE"}],
                "contextual_relevancy": 0.71,
                "contextual_recall": 0.72,
                "contextual_precision": 0.73,
                "faithfulness": 0.74,
                "answer_relevancy": 0.75,
                "answer_correctness": 0.76,
                "citation_validity": 0.77,
                "abstention_correct": None,
            },
            {
                "id": "descriptive-abstention-case",
                "query": "SECOND SECRET QUERY",
                "contextual_relevancy": 0.61,
                "contextual_recall": None,
                "contextual_precision": 0.63,
                "faithfulness": None,
                "answer_relevancy": 0.65,
                "answer_correctness": 0.66,
                "citation_validity": 0.67,
                "abstention_correct": True,
            },
        ],
    }
    report.update(overrides)
    return report


def test_exposition_contains_all_scores_and_only_hashed_identifiers():
    report = _report()

    current, success = build_exposition(report)

    for metric in quality_metrics.AGGREGATE_METRICS.values():
        assert f'metric="{metric}"' in current
    opaque_id = hashlib.sha256(b"descriptive-password-policy-case").hexdigest()[:12]
    assert f'case_id="{opaque_id}"' in current
    assert "descriptive-password-policy-case" not in current
    for sensitive in (
        "SECRET QUERY TEXT",
        "SECRET ANSWER TEXT",
        "SECRET CONTEXT",
        "SECRET SOURCE",
        "dave-admin-sensitive-name",
        "agent-secret-id",
        "internal-model-name",
        "qca-v1",
    ):
        assert sensitive not in current
    assert "nexus_rag_quality_evaluation_regression_status -1" in current
    assert success is not None
    assert "nexus_rag_quality_evaluation_last_success_timestamp_seconds" in success


def test_undetermined_abstention_count_is_published_alongside_the_score():
    """#383 excludes undetermined abstention verdicts from abstention_accuracy
    rather than failing the run, so the score alone cannot show partial
    coverage: one determined pass plus one undetermined case still reads 1.0.
    The count is what makes that readable.
    """
    current, _ = build_exposition(_report(abstention_undetermined=1, abstention_accuracy=1.0))

    assert "nexus_rag_quality_evaluation_abstention_undetermined 1" in current
    assert 'nexus_rag_quality_evaluation_score{metric="abstention_accuracy"} 1' in current


def test_report_without_the_undetermined_field_publishes_zero():
    """Reports written before #383's fix have no such field. Under the old
    behaviour an undetermined verdict raised, so a report that exists at all had
    none -- 0 is the honest value, and failing closed here would reject every
    historical report in a --history-dir.
    """
    report = _report()
    report.pop("abstention_undetermined", None)

    current, _ = build_exposition(report)

    assert "nexus_rag_quality_evaluation_abstention_undetermined 0" in current


def test_undetermined_abstention_count_rejects_malformed_values():
    """Defaulting a missing field must not soften a present-but-garbage one."""
    with pytest.raises(ReportError, match="abstention_undetermined"):
        build_exposition(_report(abstention_undetermined=-1))


def test_comparable_baseline_publishes_deltas_and_regression():
    baseline = _report(mean_faithfulness=0.95, mean_answer_correctness=0.80)

    current, _ = build_exposition(_report(), baseline, regression_tolerance=0.05)

    assert (
        'nexus_rag_quality_evaluation_baseline_delta{metric="faithfulness"} -0.10999999999999999'
    ) in current
    assert (
        'nexus_rag_quality_evaluation_baseline_delta{metric="answer_correctness"} '
        "0.059999999999999942"
    ) in current
    assert "nexus_rag_quality_evaluation_baseline_comparable 1" in current
    assert "nexus_rag_quality_evaluation_regression_status 1" in current


def test_incompatible_baseline_omits_deltas_and_marks_regression_unknown():
    baseline = _report(judge_prompt_version="qca-v2")

    current, _ = build_exposition(_report(), baseline)

    assert "nexus_rag_quality_evaluation_baseline_delta" not in current
    assert "nexus_rag_quality_evaluation_baseline_comparable 0" in current
    assert "nexus_rag_quality_evaluation_regression_status -1" in current


def test_invalid_run_does_not_replace_last_success_group():
    current, success = build_exposition(
        _report(
            scored_query_count=1,
            tool_call_failures=1,
            errors=[{"id": "case", "error": "must not leak"}],
            queries=_report()["queries"][:1],
        )
    )

    assert "nexus_rag_quality_evaluation_valid 0" in current
    assert "must not leak" not in current
    assert success is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"golden_set_sha256": "not-a-digest"}, "lowercase SHA-256"),
        ({"mean_faithfulness": 1.1}, "between 0 and 1"),
        ({"query_count": -1}, "non-negative integer"),
        ({"timestamp": "2026-08-04T12:30:00"}, "timezone"),
        ({"queries": ["not-an-object"]}, "must be an object"),
    ],
)
def test_unsafe_or_malformed_reports_fail_closed(overrides, message):
    if "queries" in overrides:
        overrides["query_count"] = 1
        overrides["scored_query_count"] = 1

    with pytest.raises(ReportError, match=message):
        build_exposition(_report(**overrides))


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "file:///tmp/gateway",
        "https://user:password@example.test",
        "https://example.test/path?token=secret",
        "https://example.test/path#fragment",
    ],
)
def test_gateway_url_rejects_unsafe_forms(url):
    with pytest.raises(ReportError):
        quality_metrics._gateway_url(url, "job", "default")


def test_profile_is_bounded_safe_vocabulary():
    assert quality_metrics._gateway_url("http://localhost:9091", "job", "nightly_a") == (
        "http://localhost:9091/metrics/job/job/profile/nightly_a"
    )
    with pytest.raises(ReportError, match="profile must match"):
        quality_metrics._gateway_url("http://localhost:9091", "job", "../../secret")


def test_publish_puts_current_and_last_success_without_following_redirects(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(quality_metrics.httpx, "Client", client_factory)

    publish(
        "https://pushgateway.example.test/base",
        "nightly",
        "current 1\n",
        "success 1\n",
        bearer_token="token-value",
    )

    assert [request.method for request in requests] == ["PUT", "PUT"]
    assert requests[0].url.path.endswith(
        "/metrics/job/nexus-rag-quality-evaluation/profile/nightly"
    )
    assert requests[1].url.path.endswith(
        "/metrics/job/nexus-rag-quality-evaluation-last-success/profile/nightly"
    )
    assert all(request.headers["authorization"] == "Bearer token-value" for request in requests)


def test_configuration_fingerprint_changes_without_exposing_raw_configuration():
    current_a, _ = build_exposition(_report())
    current_b, _ = build_exposition(_report(judge_model="different-secret-model"))

    line_a = next(
        line
        for line in current_a.splitlines()
        if line.startswith("nexus_rag_quality_evaluation_configuration_fingerprint ")
    )
    line_b = next(
        line
        for line in current_b.splitlines()
        if line.startswith("nexus_rag_quality_evaluation_configuration_fingerprint ")
    )
    assert line_a != line_b
    assert "different-secret-model" not in current_b


def test_evaluator_report_feeds_the_publisher_without_losing_abstention_coverage(monkeypatch):
    """Interop guard across #383 and #384.

    These two halves are separate scripts with no import between them -- the
    evaluator writes JSON, the publisher reads it. That seam is exactly how
    `abstention_undetermined` was silently dropped when it was added on one side
    only: the publisher iterates a fixed allowlist, so a new field costs nothing
    and reports nothing. Neither side's own tests could see it.

    Builds a real report through the evaluator's aggregate path -- one abstention
    the judge decided, one it declined -- and asserts the published exposition
    carries both the score and the coverage count. Without the count the gauge
    reads 1.0 for a run that only determined half its abstention cases.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import evaluate_rag_quality as qca

    verdicts = iter([True, None])
    undetermined = iter([False, True])

    def fake_case(case, *, token, **kwargs):
        return {
            "id": case["id"],
            "rag_search_called": True,
            "contextual_relevancy": 0.8,
            "contextual_recall": None,
            "contextual_precision": 0.8,
            "faithfulness": None,
            "answer_relevancy": 0.9,
            "answer_correctness": 0.9,
            "citation_validity": 1.0,
            "abstention_correct": next(verdicts),
            "abstention_undetermined": next(undetermined),
        }

    monkeypatch.setattr(qca, "evaluate_case", fake_case)

    report = qca.evaluate(
        [
            {"id": "abstain-1", "query": "a", "reference_answer": ""},
            {"id": "abstain-2", "query": "b", "reference_answer": ""},
        ],
        token_provider=lambda: "token",
        agent_id="agent",
        user="dave-admin",
        judge=lambda payload: payload,
        judge_model="qwen2.5:3b-instruct",
    )

    assert report["abstention_accuracy"] == 1.0
    assert report["abstention_undetermined"] == 1

    current, _ = build_exposition(report)

    assert 'nexus_rag_quality_evaluation_score{metric="abstention_accuracy"} 1' in current
    assert "nexus_rag_quality_evaluation_abstention_undetermined 1" in current
