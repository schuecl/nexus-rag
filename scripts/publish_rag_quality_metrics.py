#!/usr/bin/env python3
"""Publish sanitized Q-to-C-to-A evaluation results for Prometheus.

Issue #384 complements #74/#383. ``evaluate_rag_quality.py`` is a short-lived
host-side process, so Prometheus cannot reliably scrape it while it runs. This
publisher reads one completed JSON report and PUTs an allowlisted, content-free
OpenMetrics payload to a Pushgateway-compatible endpoint.

Only numeric scores/counts and hashed identifiers leave the report. Query text,
answers, retrieved context, source names, user/persona names, model names, error
text, and document identifiers are never emitted. Treat the Pushgateway as a
write endpoint: keep it inside the accreditation boundary and do not expose it
without an approved authentication/network-control layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

AGGREGATE_METRICS = {
    "mean_contextual_relevancy": "contextual_relevancy",
    "mean_contextual_recall": "contextual_recall",
    "mean_contextual_precision": "contextual_precision",
    "mean_faithfulness": "faithfulness",
    "mean_answer_relevancy": "answer_relevancy",
    "mean_answer_correctness": "answer_correctness",
    "mean_citation_validity": "citation_validity",
    "abstention_accuracy": "abstention_accuracy",
}
CASE_METRICS = {
    "contextual_relevancy": "contextual_relevancy",
    "contextual_recall": "contextual_recall",
    "contextual_precision": "contextual_precision",
    "faithfulness": "faithfulness",
    "answer_relevancy": "answer_relevancy",
    "answer_correctness": "answer_correctness",
    "citation_validity": "citation_validity",
    "abstention_correct": "abstention_accuracy",
}
COMPARABILITY_FIELDS = ("judge_model", "judge_prompt_version", "golden_set_sha256")
# #386: published, but never differenced against a baseline. The judge cannot
# reliably tell a real abstention failure from a lucky pass -- shown an answer
# that invented a policy instead of abstaining, qwen2.5:3b-instruct scored it a
# correct abstention 2 times in 3 and qwen2.5:7b-instruct 3 times in 3. A delta
# between two such numbers is noise, and worse, it feeds regression_status.
#
# This mirrors evaluate_rag_quality.py's _COMPARED_METRICS, which excludes the
# same metric for the same reason. The two have to agree: the evaluator deciding
# a metric is not comparable while the dashboard draws a delta for it would be a
# disagreement the operator has no way to see.
NON_COMPARABLE_METRICS = frozenset({"abstention_accuracy"})
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_GATEWAY = os.environ.get("RAG_QUALITY_PUSHGATEWAY_URL", "http://127.0.0.1:9092")
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class ReportError(ValueError):
    """The report cannot be safely represented by the fixed metric schema."""


def _bounded_text(value: Any, field: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ReportError(f"{field} must be a non-empty string no longer than {limit} chars")
    return value


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError(f"{field} must be a non-negative integer")
    return value


def _score(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError(f"{field} must be a number between 0 and 1 or null")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ReportError(f"{field} must be a finite number between 0 and 1")
    return result


def _timestamp(value: Any) -> float:
    text = _bounded_text(value, "timestamp", limit=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportError("timestamp must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ReportError("timestamp must include a timezone")
    return parsed.timestamp()


def _hash_id(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def _configuration(report: dict[str, Any]) -> tuple[str, int, dict[str, str]]:
    judge = _bounded_text(report.get("judge_model"), "judge_model")
    prompt = _bounded_text(report.get("judge_prompt_version"), "judge_prompt_version")
    golden = _bounded_text(report.get("golden_set_sha256"), "golden_set_sha256", limit=64)
    if not SHA256_RE.fullmatch(golden):
        raise ReportError("golden_set_sha256 must be a lowercase SHA-256 digest")

    config_digest = hashlib.sha256(
        json.dumps([judge, prompt, golden], separators=(",", ":")).encode()
    ).hexdigest()
    # Thirteen hex digits are 52 bits, exactly representable by a Prometheus
    # float. The gauge changes when any comparability field changes and powers
    # the dashboard annotation without exposing any raw configuration value.
    fingerprint = int(config_digest[:13], 16)
    labels = {
        "config_id": config_digest[:12],
        "judge_id": _hash_id(judge),
        "prompt_id": _hash_id(prompt),
        "golden_set_id": golden[:12],
    }
    return config_digest, fingerprint, labels


def _metric_line(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    suffix = ""
    if labels:
        # All label values passed here are fixed vocabulary or lowercase hex.
        # Keeping this assertion beside serialization prevents a future caller
        # from accidentally adding report content without an explicit review.
        for label_value in labels.values():
            if not re.fullmatch(r"[a-z0-9_-]+", label_value):
                raise ReportError("metric labels must contain only sanitized identifiers")
        joined = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
        suffix = f"{{{joined}}}"
    return f"{name}{suffix} {value:.17g}" if isinstance(value, float) else f"{name}{suffix} {value}"


def _family(name: str, help_text: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", *lines]


def _comparable(current: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return all(current.get(field) == baseline.get(field) for field in COMPARABILITY_FIELDS)


def build_exposition(
    report: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    *,
    regression_tolerance: float = 0.05,
) -> tuple[str, str | None]:
    """Return current-run and optional last-success Prometheus payloads."""
    if not isinstance(report, dict):
        raise ReportError("report must be a JSON object")
    if not math.isfinite(regression_tolerance) or regression_tolerance < 0:
        raise ReportError("regression_tolerance must be a finite non-negative number")

    run_timestamp = _timestamp(report.get("timestamp"))
    _, fingerprint, config_labels = _configuration(report)
    query_count = _count(report.get("query_count"), "query_count")
    scored_query_count = _count(report.get("scored_query_count"), "scored_query_count")
    tool_failures = _count(report.get("tool_call_failures"), "tool_call_failures")
    # #383: abstention verdicts the judge would not commit to. Those cases are
    # excluded from abstention_accuracy rather than failing the run, so without
    # this count the gauge is unreadable: one determined pass plus one
    # undetermined case still reports 1.0, indistinguishable from full coverage.
    #
    # Defaulted rather than required, so a report written before #383's fix
    # publishes as 0 instead of failing closed. That is honest for those
    # reports -- under the old behaviour an undetermined verdict raised, so a
    # report that exists at all had none.
    abstention_undetermined = _count(
        report.get("abstention_undetermined", 0), "abstention_undetermined"
    )
    errors = report.get("errors")
    queries = report.get("queries")
    if not isinstance(errors, list):
        raise ReportError("errors must be a list")
    if not isinstance(queries, list):
        raise ReportError("queries must be a list")
    if scored_query_count != len(queries) or scored_query_count > query_count:
        raise ReportError("scored_query_count must match queries and not exceed query_count")

    valid = not errors and tool_failures == 0 and scored_query_count == query_count
    aggregate_values: dict[str, float | None] = {}
    for field, metric in AGGREGATE_METRICS.items():
        aggregate_values[metric] = _score(report.get(field), field)

    baseline_comparable = baseline is not None and _comparable(report, baseline)
    deltas: dict[str, float] = {}
    regressed = False
    if baseline_comparable and baseline is not None:
        _configuration(baseline)
        for field, metric in AGGREGATE_METRICS.items():
            if metric in NON_COMPARABLE_METRICS:
                continue
            current_value = aggregate_values[metric]
            baseline_value = _score(baseline.get(field), f"baseline.{field}")
            if current_value is None or baseline_value is None:
                continue
            delta = current_value - baseline_value
            deltas[metric] = delta
            regressed = regressed or delta < -regression_tolerance

    score_lines = [
        _metric_line(
            "nexus_rag_quality_evaluation_score",
            value,
            {"metric": metric},
        )
        for metric, value in aggregate_values.items()
        if value is not None
    ]
    delta_lines = [
        _metric_line(
            "nexus_rag_quality_evaluation_baseline_delta",
            value,
            {"metric": metric},
        )
        for metric, value in deltas.items()
    ]
    case_lines: list[str] = []
    for index, case in enumerate(queries):
        if not isinstance(case, dict):
            raise ReportError(f"queries[{index}] must be an object")
        raw_case_id = _bounded_text(case.get("id"), f"queries[{index}].id", limit=1024)
        opaque_case_id = _hash_id(raw_case_id)
        for field, metric in CASE_METRICS.items():
            raw_value = case.get(field)
            if field == "abstention_correct" and isinstance(raw_value, bool):
                value: float | None = float(raw_value)
            else:
                value = _score(raw_value, f"queries[{index}].{field}")
            if value is not None:
                case_lines.append(
                    _metric_line(
                        "nexus_rag_quality_evaluation_case_score",
                        value,
                        {"case_id": opaque_case_id, "metric": metric},
                    )
                )

    regression_status = -1 if not baseline_comparable else int(regressed)
    lines: list[str] = []
    lines.extend(
        _family(
            "nexus_rag_quality_evaluation_score",
            "Latest aggregate Q-to-C-to-A score; relative within one evaluation configuration.",
            score_lines,
        )
    )
    lines.extend(
        _family(
            "nexus_rag_quality_evaluation_baseline_delta",
            "Current aggregate score minus a comparable baseline score.",
            delta_lines,
        )
    )
    lines.extend(
        _family(
            "nexus_rag_quality_evaluation_case_score",
            "Latest per-case score keyed by a one-way opaque case identifier.",
            case_lines,
        )
    )

    scalar_families = [
        (
            "nexus_rag_quality_evaluation_valid",
            "Whether the latest run scored every case without tool or evaluation failures.",
            int(valid),
        ),
        (
            "nexus_rag_quality_evaluation_cases",
            "Number of cases requested by the latest evaluation run.",
            query_count,
        ),
        (
            "nexus_rag_quality_evaluation_scored_cases",
            "Number of cases scored by the latest evaluation run.",
            scored_query_count,
        ),
        (
            "nexus_rag_quality_evaluation_tool_call_failures",
            "Number of cases that did not produce recognizable rag_search output.",
            tool_failures,
        ),
        (
            "nexus_rag_quality_evaluation_errors",
            "Number of case evaluation errors in the latest run.",
            len(errors),
        ),
        (
            "nexus_rag_quality_evaluation_abstention_undetermined",
            "Abstention cases the judge would not decide; excluded from the "
            "abstention_accuracy score, so read that score against this count.",
            abstention_undetermined,
        ),
        (
            "nexus_rag_quality_evaluation_baseline_comparable",
            "Whether the supplied baseline matches judge, prompt, and golden-set identity.",
            int(baseline_comparable),
        ),
        (
            "nexus_rag_quality_evaluation_regression_status",
            "Regression state: -1 unknown, 0 no regression, 1 regression.",
            regression_status,
        ),
        (
            "nexus_rag_quality_evaluation_run_timestamp_seconds",
            "Timestamp declared by the latest published report.",
            run_timestamp,
        ),
        (
            "nexus_rag_quality_evaluation_configuration_fingerprint",
            "Opaque numeric fingerprint of judge, prompt, and golden-set identity.",
            fingerprint,
        ),
    ]
    for name, help_text, value in scalar_families:
        lines.extend(_family(name, help_text, [_metric_line(name, value)]))

    info_name = "nexus_rag_quality_evaluation_configuration_info"
    lines.extend(
        _family(
            info_name,
            "Opaque identifiers for the active evaluation configuration.",
            [_metric_line(info_name, 1, config_labels)],
        )
    )
    current_payload = "\n".join(lines) + "\n"

    success_payload: str | None = None
    if valid:
        success_name = "nexus_rag_quality_evaluation_last_success_timestamp_seconds"
        success_payload = (
            "\n".join(
                _family(
                    success_name,
                    "Timestamp of the last fully valid Q-to-C-to-A evaluation run.",
                    [_metric_line(success_name, run_timestamp)],
                )
            )
            + "\n"
        )
    return current_payload, success_payload


def _gateway_url(base_url: str, job: str, profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise ReportError("profile must match [a-z][a-z0-9_-]{0,31}")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ReportError("pushgateway URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReportError("pushgateway URL must not contain credentials, query, or fragment")
    root = base_url.rstrip("/")
    return f"{root}/metrics/job/{quote(job, safe='')}/profile/{quote(profile, safe='')}"


def publish(
    gateway_url: str,
    profile: str,
    current_payload: str,
    success_payload: str | None,
    *,
    timeout: float = 10.0,
    bearer_token: str | None = None,
    ca_file: Path | None = None,
) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ReportError("timeout must be a finite positive number")
    headers = {"Content-Type": CONTENT_TYPE}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    verify: bool | str = str(ca_file) if ca_file else True
    with httpx.Client(timeout=timeout, verify=verify, follow_redirects=False) as client:
        response = client.put(
            _gateway_url(gateway_url, "nexus-rag-quality-evaluation", profile),
            content=current_payload,
            headers=headers,
        )
        response.raise_for_status()
        if success_payload is not None:
            response = client.put(
                _gateway_url(
                    gateway_url,
                    "nexus-rag-quality-evaluation-last-success",
                    profile,
                ),
                content=success_payload,
                headers=headers,
            )
            response.raise_for_status()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read JSON report {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"{path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--pushgateway-url", default=DEFAULT_GATEWAY)
    parser.add_argument("--regression-tolerance", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--bearer-token-file", type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        report = _load_json(args.report)
        baseline = _load_json(args.baseline) if args.baseline else None
        current_payload, success_payload = build_exposition(
            report,
            baseline,
            regression_tolerance=args.regression_tolerance,
        )
        if args.dry_run:
            print(current_payload, end="")
            if success_payload:
                print("# Last-success group")
                print(success_payload, end="")
            return 0

        token = None
        if args.bearer_token_file:
            token = args.bearer_token_file.read_text().strip()
            if not token:
                raise ReportError("bearer token file is empty")
        publish(
            args.pushgateway_url,
            args.profile,
            current_payload,
            success_payload,
            timeout=args.timeout,
            bearer_token=token,
            ca_file=args.ca_file,
        )
    except (ReportError, OSError, httpx.HTTPError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Published sanitized Q-to-C-to-A metrics for profile {args.profile!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
