"""Issue #111: the guard that keeps Compose's hardening aligned with the chart.

A lint nobody tests is a lint that quietly stops catching things -- these pin
down that each rule actually fails on the shape it exists to reject, not just
that the current docker-compose.yml happens to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_compose_hardening import _check_service

HARDENED = {
    "user": "10001:10001",
    "read_only": True,
    "tmpfs": ["/tmp:size=64m"],
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
}


def _custom(**overrides) -> dict:
    spec = dict(HARDENED)
    spec.update(overrides)
    return spec


class TestCustomServices:
    def test_a_fully_hardened_service_passes(self):
        assert _check_service("ingestion-api", _custom()) == []

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("user", None, "user must be"),
            ("user", "0:0", "user must be"),
            ("read_only", False, "read_only must be true"),
            ("cap_drop", [], "cap_drop must include ALL"),
            ("security_opt", [], "no-new-privileges"),
        ],
    )
    def test_each_missing_setting_is_reported(self, field, value, expected):
        problems = _check_service("ingestion-api", _custom(**{field: value}))

        assert any(expected in p for p in problems), problems

    def test_read_only_without_a_tmpfs_is_rejected(self):
        """read_only with nowhere to write is the shape that starts fine and
        then fails at the first scratch-file write, which is exactly the
        failure this issue wants surfacing in dev."""
        problems = _check_service("ingestion-api", _custom(tmpfs=None))

        assert any("no tmpfs" in p for p in problems), problems

    def test_an_unsized_tmpfs_is_rejected(self):
        """Issue #209: unlike the chart's emptyDir (already bounded by
        ephemeral-storage limits), Compose's tmpfs is RAM-backed -- an
        unsized mount lets an oversized upload's multipart spool consume
        host memory before MAX_UPLOAD_BYTES is ever checked."""
        problems = _check_service("ingestion-api", _custom(tmpfs=["/tmp"]))

        assert any("no size=" in p for p in problems), problems

    def test_a_sized_tmpfs_passes(self):
        assert _check_service("ingestion-api", _custom(tmpfs=["/tmp:size=64m"])) == []

    def test_a_long_form_tmpfs_entry_is_reported_not_silently_passed(self):
        problems = _check_service(
            "ingestion-api", _custom(tmpfs=[{"target": "/tmp", "tmpfs": {"size": 67108864}}])
        )

        assert any("long-form tmpfs" in p for p in problems), problems


class TestEveryService:
    def test_third_party_services_need_only_no_new_privileges(self):
        # cap_drop is deliberately not required: postgres and keycloak drop
        # privileges from root at startup and need CAP_CHOWN/SETUID/SETGID.
        assert _check_service("postgres", {"security_opt": ["no-new-privileges:true"]}) == []

    def test_a_third_party_service_without_it_is_reported(self):
        problems = _check_service("postgres", {"image": "postgres:16.14"})

        assert any("no-new-privileges" in p for p in problems), problems

    def test_one_shot_containers_are_exempt(self):
        """`run --rm` containers hold no listening port and some need to write
        as root -- exempt by name so the exemption is visible, not implied."""
        assert _check_service("seed-sample-data", {}) == []


class TestPortBinding:
    def test_a_port_bound_to_every_interface_is_reported(self):
        problems = _check_service("reranker-service", _custom(ports=["8003:8003"]))

        assert any("binds all interfaces" in p for p in problems), problems

    def test_an_explicit_host_address_passes(self):
        assert _check_service("reranker-service", _custom(ports=["127.0.0.1:8003:8003"])) == []
