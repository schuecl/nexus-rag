from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from publish_rag_quality_metrics import build_exposition  # noqa: E402

_HELP_RE = re.compile(r"^# HELP (\S+) ", re.MULTILINE)
INFRA_DASHBOARD = (
    REPO_ROOT
    / "infra"
    / "observability"
    / "grafana"
    / "dashboards"
    / "nexus-rag-quality-evaluation.json"
)
HELM_DASHBOARD = REPO_ROOT / "helm" / "observability" / "dashboards" / INFRA_DASHBOARD.name


def _dashboard():
    return json.loads(INFRA_DASHBOARD.read_text())


def _published_metric_families():
    """Every metric family the publisher emits for a representative report.

    A comparable baseline is supplied so the baseline_delta family is present;
    without one the publisher legitimately omits it.
    """
    report = {
        "timestamp": "2026-08-04T12:30:00+00:00",
        "golden_set_sha256": "a" * 64,
        "judge_model": "judge",
        "judge_prompt_version": "qca-v2",
        "query_count": 1,
        "scored_query_count": 1,
        "tool_call_failures": 0,
        "errors": [],
        "abstention_undetermined": 1,
        "mean_contextual_relevancy": 0.8,
        "mean_contextual_recall": 0.8,
        "mean_contextual_precision": 0.8,
        "mean_faithfulness": 0.8,
        "mean_answer_relevancy": 0.8,
        "mean_answer_correctness": 0.8,
        "mean_citation_validity": 0.8,
        "abstention_accuracy": 1.0,
        "queries": [{"id": "case-1", "contextual_relevancy": 0.8, "abstention_correct": True}],
    }
    current, success = build_exposition(report, dict(report))
    return set(_HELP_RE.findall(current)) | set(_HELP_RE.findall(success))


def _expressions(dashboard):
    """Panel targets *and* annotation queries.

    Annotations are a real consumer -- the configuration-change marker is driven
    entirely by one -- so omitting them here would report a metric as unused
    when it is the only thing a panel-only view would miss.
    """
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    expressions += [
        annotation["expr"]
        for annotation in dashboard.get("annotations", {}).get("list", [])
        if "expr" in annotation
    ]
    return expressions


def test_dashboard_is_valid_mirrored_asset():
    assert INFRA_DASHBOARD.read_bytes() == HELM_DASHBOARD.read_bytes()
    dashboard = _dashboard()
    assert dashboard["uid"] == "nexus-rag-quality-evaluation"
    assert dashboard["title"] == "Nexus RAG / RAG quality evaluation"
    assert dashboard["schemaVersion"] == 39


def test_dashboard_covers_required_visualizations():
    dashboard = _dashboard()
    panel_types = {panel["type"] for panel in dashboard["panels"]}

    assert {"stat", "gauge", "timeseries", "bargauge", "table", "row"} <= panel_types
    assert {panel["title"] for panel in dashboard["panels"]} >= {
        "Latest quality scores",
        "Quality-score trends",
        "Current vs comparable baseline",
        "Run validity",
        "Baseline regression",
        "Tool-call failures",
        "Evaluation errors",
        "Opaque case-level scores",
        "Opaque configuration identity",
    }


def test_dashboard_queries_every_sanitized_metric_family():
    """Derived from what the publisher actually emits, not from a hand-kept list.

    A hardcoded list cannot fail when a new metric family is added without a
    panel -- which is exactly how a published metric ends up invisible. Reading
    the families out of a real exposition payload makes adding one to the
    publisher and forgetting the dashboard a test failure.
    """
    # Published deliberately without a panel of its own, because the dashboard
    # already shows the same information: regression_status is -1 exactly when
    # the baseline is not comparable, so a dedicated panel would be a second
    # rendering of one bit. Kept in the exposition because an alerting rule or
    # an ad-hoc query is a different consumer from this dashboard.
    not_visualized = {"nexus_rag_quality_evaluation_baseline_comparable"}

    published = _published_metric_families() - not_visualized
    assert published, "publisher produced no metric families"

    expressions = "\n".join(_expressions(_dashboard()))
    missing = sorted(metric for metric in published if metric not in expressions)

    assert not missing, (
        f"published but never queried by the dashboard: {missing}. "
        "Add a panel, or stop publishing the metric."
    )


def test_dashboard_has_profile_selector_and_configuration_change_annotation():
    dashboard = _dashboard()
    variables = dashboard["templating"]["list"]
    annotations = dashboard["annotations"]["list"]

    assert len(variables) == 1
    assert variables[0]["name"] == "profile"
    assert variables[0]["includeAll"] is True
    assert all("$profile" in expression for expression in _expressions(dashboard))
    assert len(annotations) == 1
    assert (
        "changes(nexus_rag_quality_evaluation_configuration_fingerprint" in annotations[0]["expr"]
    )


def test_quality_gauge_does_not_claim_an_absolute_red_green_threshold():
    dashboard = _dashboard()
    gauge = next(
        panel for panel in dashboard["panels"] if panel["title"] == "Latest quality scores"
    )

    assert gauge["fieldConfig"]["defaults"]["color"] == {
        "fixedColor": "blue",
        "mode": "fixed",
    }
    assert gauge["fieldConfig"]["defaults"]["thresholds"]["steps"] == [
        {"color": "blue", "value": None}
    ]
