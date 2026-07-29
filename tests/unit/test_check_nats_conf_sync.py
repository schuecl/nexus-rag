"""Issue #212: the guard that keeps infra/nats/nats.conf and
helm/nexus-rag/files/nats.conf from drifting apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_nats_conf_sync import _violation


class TestNatsConfSyncCheck:
    def test_identical_files_pass(self, tmp_path):
        a = tmp_path / "a.conf"
        b = tmp_path / "b.conf"
        a.write_text("authorization { users = [] }\n")
        b.write_text("authorization { users = [] }\n")

        assert _violation(a, b) is None

    def test_drifted_files_are_reported(self, tmp_path):
        a = tmp_path / "a.conf"
        b = tmp_path / "b.conf"
        a.write_text("authorization { users = [] }\n")
        b.write_text('authorization { users = [{user: "x"}] }\n')

        problem = _violation(a, b)

        assert problem is not None
        assert "drifted apart" in problem
