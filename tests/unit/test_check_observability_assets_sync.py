"""Issue #257: the guard that keeps the Grafana dashboards and Prometheus alert
rules from drifting between infra/observability/ (mounted by docker-compose) and
helm/observability/ (vendored into the chart).

Same shape as test_check_nats_conf_sync.py -- the underlying risk is identical,
only the asset differs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_observability_assets_sync import MIRRORED, _violations


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "infra"
    mirror = tmp_path / "chart"
    source.mkdir()
    mirror.mkdir()
    return source, mirror


class TestObservabilityAssetSyncCheck:
    def test_identical_trees_pass(self, tmp_path):
        source, mirror = _pair(tmp_path)
        (source / "board.json").write_text('{"uid": "x"}\n')
        (mirror / "board.json").write_text('{"uid": "x"}\n')

        assert _violations(source, mirror, "*") == []

    def test_drifted_file_is_reported(self, tmp_path):
        source, mirror = _pair(tmp_path)
        (source / "board.json").write_text('{"uid": "x", "panels": 10}\n')
        (mirror / "board.json").write_text('{"uid": "x", "panels": 5}\n')

        problems = _violations(source, mirror, "*")

        assert len(problems) == 1
        assert "drifted apart" in problems[0]

    def test_file_missing_from_the_chart_is_reported(self, tmp_path):
        source, mirror = _pair(tmp_path)
        (source / "new-board.json").write_text("{}\n")

        problems = _violations(source, mirror, "*")

        assert len(problems) == 1
        assert "no counterpart" in problems[0]

    def test_chart_only_file_is_reported(self, tmp_path):
        """A dashboard that exists only in the chart is never exercised by the
        Compose stack or the e2e job, so it is a violation rather than a bonus.
        """
        source, mirror = _pair(tmp_path)
        (mirror / "orphan.json").write_text("{}\n")

        problems = _violations(source, mirror, "*")

        assert len(problems) == 1
        assert "only in the chart" in problems[0]

    def test_glob_pattern_is_respected(self, tmp_path):
        """The rules pair matches *.yml, so a stray README on one side alone is
        not a violation.
        """
        source, mirror = _pair(tmp_path)
        (source / "rules.yml").write_text("groups: []\n")
        (mirror / "rules.yml").write_text("groups: []\n")
        (source / "README.md").write_text("notes\n")

        assert _violations(source, mirror, "*.yml") == []

    def test_missing_mirror_directory_is_reported(self, tmp_path):
        source = tmp_path / "infra"
        source.mkdir()

        problems = _violations(source, tmp_path / "absent", "*")

        assert len(problems) == 1
        assert "missing its vendored copy" in problems[0]

    def test_binary_assets_are_compared_too(self, tmp_path):
        """system-flow.svg travels with the dashboards; comparing bytes rather
        than decoded text is what lets a non-UTF-8 asset be checked at all.
        """
        source, mirror = _pair(tmp_path)
        (source / "flow.svg").write_bytes(b"\x89PNG\x00binary")
        (mirror / "flow.svg").write_bytes(b"\x89PNG\x00different")

        problems = _violations(source, mirror, "*")

        assert len(problems) == 1
        assert "drifted apart" in problems[0]


class TestRealRepositoryAssets:
    def test_configured_paths_all_exist(self):
        """Guards that silently stop checking anything are worse than no guard,
        so assert the configured directories are real rather than trusting them.
        """
        for source, mirror, _pattern in MIRRORED:
            assert source.is_dir(), f"{source} is not a directory"
            assert mirror.is_dir(), f"{mirror} is not a directory"

    def test_repository_is_currently_in_sync(self):
        problems: list[str] = []
        for source, mirror, pattern in MIRRORED:
            problems.extend(_violations(source, mirror, pattern))

        assert problems == [], "\n".join(problems)
