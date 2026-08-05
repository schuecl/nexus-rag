"""Issue #426 (#127 gap #4): unit coverage for the pure aggregation and
exposition logic in scripts/detect_query_anomalies.py -- mining `query`/
`query.denied` audit rows for reconnaissance-shaped patterns. The DB fetch
itself (`fetch_query_events`) needs a live Postgres with the dedicated
audit-reporting role and is not exercised here, same split
`test_calibrate_tagging_advisory.py` uses: these tests build the same
row shape `fetch_query_events` would return and feed it straight to
`aggregate`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import detect_query_anomalies as dqa
from detect_query_anomalies import aggregate, build_exposition, publish

T0 = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def _event(
    actor_sub: str,
    action: str,
    *,
    offset_seconds: float = 0,
    result_count: int | None = None,
    actor_username: str | None = None,
) -> dict:
    detail = {} if result_count is None else {"result_count": result_count}
    return {
        "actor_sub": actor_sub,
        "actor_username": actor_username or actor_sub,
        "action": action,
        "detail": detail,
        "created_at": T0 + timedelta(seconds=offset_seconds),
    }


class TestHighVolume:
    def test_flags_identity_at_or_above_rate_threshold(self):
        events = [_event("alice", "query", result_count=3, offset_seconds=i) for i in range(5)]
        report = aggregate(events, window_seconds=3600, rate_threshold=5)
        assert report.identities["alice"].signals == ["high_volume"]

    def test_does_not_flag_below_threshold(self):
        events = [_event("alice", "query", result_count=3, offset_seconds=i) for i in range(4)]
        report = aggregate(events, window_seconds=3600, rate_threshold=5)
        assert report.identities["alice"].signals == []


class TestHighDenialRatio:
    def test_flags_sustained_denial_rate(self):
        events = [_event("bob", "query.denied", offset_seconds=i) for i in range(8)] + [
            _event("bob", "query", result_count=2, offset_seconds=8)
        ]
        report = aggregate(
            events,
            window_seconds=3600,
            rate_threshold=1000,
            denial_ratio_threshold=0.5,
            min_attempts_for_ratio=5,
        )
        assert "high_denial_ratio" in report.identities["bob"].signals

    def test_single_denial_is_not_flagged_below_min_attempts(self):
        events = [_event("carol", "query.denied", offset_seconds=0)]
        report = aggregate(
            events,
            window_seconds=3600,
            denial_ratio_threshold=0.5,
            min_attempts_for_ratio=10,
        )
        assert report.identities["carol"].signals == []


class TestNarrowProbeShaped:
    def test_flags_mostly_singleton_or_empty_results(self):
        events = [
            _event("dave", "query", result_count=(0 if i % 2 == 0 else 1), offset_seconds=i)
            for i in range(12)
        ]
        report = aggregate(
            events,
            window_seconds=3600,
            singleton_threshold=0.5,
            min_success_for_singleton=10,
        )
        assert "narrow_probe_shaped" in report.identities["dave"].signals

    def test_normal_result_counts_are_not_flagged(self):
        events = [_event("erin", "query", result_count=5, offset_seconds=i) for i in range(12)]
        report = aggregate(
            events,
            window_seconds=3600,
            singleton_threshold=0.5,
            min_success_for_singleton=10,
        )
        assert "narrow_probe_shaped" not in report.identities["erin"].signals


class TestBoundaryMapping:
    def test_flags_repeated_denial_then_success_sequences(self):
        events = []
        for i in range(6):
            base = i * 100
            events.append(_event("frank", "query.denied", offset_seconds=base))
            events.append(_event("frank", "query", result_count=1, offset_seconds=base + 10))
        report = aggregate(events, window_seconds=3600, sequence_threshold=5)
        stats = report.identities["frank"]
        assert stats.boundary_mapping_count == 6
        assert "boundary_mapping" in stats.signals

    def test_success_outside_sequence_window_does_not_count(self):
        events = [
            _event("grace", "query.denied", offset_seconds=0),
            _event("grace", "query", result_count=1, offset_seconds=1000),
        ]
        report = aggregate(events, window_seconds=3600, sequence_window_seconds=300)
        assert report.identities["grace"].boundary_mapping_count == 0

    def test_unrelated_success_with_no_prior_denial_does_not_count(self):
        events = [_event("heidi", "query", result_count=1, offset_seconds=0)]
        report = aggregate(events, window_seconds=3600)
        assert report.identities["heidi"].boundary_mapping_count == 0


class TestFlaggedCountsAreIdentityFree:
    def test_flagged_counts_has_no_identity_information(self):
        events = [_event("ivan", "query", result_count=3, offset_seconds=i) for i in range(50)]
        report = aggregate(events, window_seconds=3600, rate_threshold=5)
        counts = report.flagged_counts()
        assert counts["high_volume"] == 1
        assert "ivan" not in str(counts)


class TestBuildExposition:
    def test_exposition_never_contains_actor_identity(self):
        events = [
            _event("judy-sensitive-name", "query", result_count=3, offset_seconds=i)
            for i in range(40)
        ]
        report = aggregate(events, window_seconds=3600, rate_threshold=5)
        payload = build_exposition(report, run_timestamp=1_700_000_000.0)
        assert "judy-sensitive-name" not in payload

    def test_exposition_reports_flagged_counts_and_metadata(self):
        events = [_event("kim", "query", result_count=3, offset_seconds=i) for i in range(10)]
        report = aggregate(events, window_seconds=1800, rate_threshold=5)
        payload = build_exposition(report, run_timestamp=1_700_000_000.0)
        assert 'nexus_rag_query_anomaly_flagged_identities{signal="high_volume"} 1' in payload
        assert "nexus_rag_query_anomaly_max_attempts 10" in payload
        assert "nexus_rag_query_anomaly_window_seconds 1800" in payload
        assert "nexus_rag_query_anomaly_last_run_timestamp_seconds 1700000000" in payload


class TestPublish:
    def test_publish_puts_to_the_job_specific_gateway_path(self, monkeypatch):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(202, request=request)

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(dqa.httpx, "Client", client_factory)

        publish("http://pushgateway.example.test", "metric 1\n")

        assert len(requests) == 1
        assert requests[0].method == "PUT"
        assert requests[0].url.path == "/metrics/job/nexus-rag-query-anomaly-detection"

    def test_publish_raises_on_gateway_error(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, request=request)

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(dqa.httpx, "Client", client_factory)

        with pytest.raises(httpx.HTTPStatusError):
            publish("http://pushgateway.example.test", "metric 1\n")
