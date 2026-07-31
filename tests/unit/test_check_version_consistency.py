"""Issue #295: the guard that keeps the stack's lockstep version consistent.

Same spirit as test_check_pinned_models.py -- pin down that the check
actually fails on the shapes it exists to reject (drifted file, wrong
expected tag, mangled repository name), not just that the current source
tree happens to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import check_version_consistency as cvc


def _write_tree(root: Path, *, worker_version: str = "0.1.0", api_tag: str = "0.1.0"):
    """A minimal repo tree with every location the script reads."""
    chart = root / "helm/nexus-rag"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text('version: 0.1.0\nappVersion: "0.1.0"\n')
    values = "\n".join(
        f"{key}:\n  image:\n    repository: {name}\n    tag: "
        + (f'"{api_tag}"' if key == "ingestionApi" else '"0.1.0"')
        for key, name in cvc.CHART_IMAGE_COMPONENTS.items()
    )
    (chart / "values.yaml").write_text(values + "\n")
    for service in cvc.SERVICES:
        pkg = root / "services" / service
        pkg.mkdir(parents=True)
        version = worker_version if service == "ingestion-worker" else "0.1.0"
        (pkg / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')


class TestVersionConsistency:
    def test_consistent_tree_passes(self, tmp_path, monkeypatch, capsys):
        _write_tree(tmp_path)
        monkeypatch.setattr(cvc, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["check_version_consistency.py"])

        assert cvc.main() == 0
        assert "agree on 0.1.0" in capsys.readouterr().out

    def test_one_drifted_pyproject_fails_and_names_it(self, tmp_path, monkeypatch, capsys):
        _write_tree(tmp_path, worker_version="0.2.0")
        monkeypatch.setattr(cvc, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["check_version_consistency.py"])

        assert cvc.main() == 1
        out = capsys.readouterr().out
        assert "not consistent" in out
        assert "services/ingestion-worker/pyproject.toml: 0.2.0" in out

    def test_drifted_chart_image_tag_fails(self, tmp_path, monkeypatch, capsys):
        _write_tree(tmp_path, api_tag="0.0.9")
        monkeypatch.setattr(cvc, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["check_version_consistency.py"])

        assert cvc.main() == 1
        assert "ingestionApi.image.tag: 0.0.9" in capsys.readouterr().out

    def test_expected_version_mismatch_fails(self, tmp_path, monkeypatch, capsys):
        # release.yml passes the tag (v-stripped); a tag ahead of the files
        # must refuse, pointing at the release-PR-first procedure.
        _write_tree(tmp_path)
        monkeypatch.setattr(cvc, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["check_version_consistency.py", "0.2.0"])

        assert cvc.main() == 1
        assert "BEFORE the tag" in capsys.readouterr().out

    def test_expected_version_match_passes(self, tmp_path, monkeypatch):
        _write_tree(tmp_path)
        monkeypatch.setattr(cvc, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["check_version_consistency.py", "0.1.0"])

        assert cvc.main() == 0

    def test_foreign_repository_name_is_rejected(self, tmp_path, monkeypatch, capsys):
        # A fully-qualified foreign repository would deploy the wrong image
        # at the right-looking version -- collect_versions refuses outright.
        _write_tree(tmp_path)
        values = tmp_path / "helm/nexus-rag/values.yaml"
        values.write_text(
            values.read_text().replace(
                "repository: ingestion-api", "repository: evil/ingestion-api"
            )
        )
        monkeypatch.setattr(cvc, "REPO_ROOT", tmp_path)

        with pytest.raises(SystemExit):
            cvc.collect_versions()
        assert "ingestionApi.image.repository" in capsys.readouterr().out

    def test_real_tree_is_currently_consistent(self, monkeypatch, capsys):
        # The actual repo must pass its own guard -- if this fails, a version
        # bump landed partially.
        monkeypatch.setattr(sys, "argv", ["check_version_consistency.py"])
        assert cvc.main() == 0
