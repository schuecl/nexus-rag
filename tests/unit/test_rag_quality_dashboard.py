from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
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


def _expressions(dashboard):
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]


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
    expressions = "\n".join(_expressions(_dashboard()))

    for metric in (
        "nexus_rag_quality_evaluation_score",
        "nexus_rag_quality_evaluation_baseline_delta",
        "nexus_rag_quality_evaluation_case_score",
        "nexus_rag_quality_evaluation_valid",
        "nexus_rag_quality_evaluation_regression_status",
        "nexus_rag_quality_evaluation_cases",
        "nexus_rag_quality_evaluation_scored_cases",
        "nexus_rag_quality_evaluation_tool_call_failures",
        "nexus_rag_quality_evaluation_errors",
        "nexus_rag_quality_evaluation_last_success_timestamp_seconds",
        "nexus_rag_quality_evaluation_configuration_info",
    ):
        assert metric in expressions


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
