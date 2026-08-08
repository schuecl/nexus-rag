"""Tests for scripts/audit_rmf_mapping.py (issue #542, GOVERN 1.5).

The script's job is to make the annual internal audit reproducible, so the tests
are mostly about the failure modes it exists to catch -- a cited file that no
longer exists, a status invented outside the documented vocabulary, and a status
regression between audits. Each of those is asserted both ways: it must fire when
the defect is present and stay silent when it is not, because a check that cannot
pass is as useless as one that cannot fail.

The fixtures are hand-written miniature mapping tables rather than the real
document. Pinning assertions to the live `rmf-mapping.md` would make these tests
fail whenever the mapping legitimately changes, which is often.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "audit_rmf_mapping", REPO_ROOT / "scripts" / "audit_rmf_mapping.py"
)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_rmf_mapping"] = audit
_spec.loader.exec_module(audit)


MAPPING_HEADER = """# NIST AI RMF mapping — outcome by outcome

Statuses use the repo convention.

## GOVERN

| Outcome | Evidence / gap | Status |
|---|---|---|
"""


def _mapping(*rows: str) -> str:
    return MAPPING_HEADER + "\n".join(rows) + "\n"


def test_rows_are_parsed_with_their_rmf_function() -> None:
    text = _mapping("| 1.1 Something | evidence | Implemented |") + (
        "\n## MEASURE\n\n| Outcome | Evidence / gap | Status |\n|---|---|---|\n"
        "| 1.3 Independence | none | Gap |\n"
    )
    rows = audit.parse_mapping(text)
    assert [(r.function, r.outcome) for r in rows] == [
        ("GOVERN", "1.1 Something"),
        ("MEASURE", "1.3 Independence"),
    ]


def test_table_header_separator_is_not_mistaken_for_a_row() -> None:
    rows = audit.parse_mapping(_mapping("| 1.1 Something | evidence | Implemented |"))
    assert len(rows) == 1, "the |---|---|---| separator must not parse as a row"


class TestReferenceIntegrity:
    def test_existing_path_resolves(self) -> None:
        assert audit.resolve_reference("REQUIREMENTS.md") == "REQUIREMENTS.md"

    def test_bare_filename_resolves_by_searching_the_tree(self) -> None:
        """The mapping cites some modules by name only (`claims.py`)."""
        resolved = audit.resolve_reference("claims.py")
        assert resolved is not None and resolved.endswith("claims.py")

    def test_missing_file_does_not_resolve(self) -> None:
        assert audit.resolve_reference("docs/no-such-file-here.md") is None

    def test_broken_reference_is_reported(self) -> None:
        text = _mapping("| 1.1 X | see `docs/definitely-not-here.md` | Implemented |")
        result = audit.assess(text, {})
        assert result["rows"][0]["broken_references"] == ["docs/definitely-not-here.md"]

    def test_intact_reference_is_not_reported(self) -> None:
        text = _mapping("| 1.1 X | see `REQUIREMENTS.md` | Implemented |")
        assert audit.assess(text, {})["rows"][0]["broken_references"] == []

    def test_an_untracked_file_never_resolves(self, tmp_path: Path) -> None:
        """The bug @schuecl found in review, pinned.

        The first version searched the working tree with `rglob`, excluding only
        `.git` and `node_modules`. With the `.venv-test` this repo's own CLAUDE.md
        tells contributors to create at the repo root, `metadata.py` resolved to
        `.venv-test/.../site-packages/.../metadata.py` -- and the run still exited
        0, so the wrong answer was invisible. The committed baseline.json carried
        that path.

        Resolution is now restricted to git-tracked files, so anything present in
        the working tree but not in the commit -- a venv, a build directory, a
        cache -- cannot be cited. Written against a real untracked file rather
        than a mock, because a mock of `git ls-files` would have passed under the
        old implementation too.
        """
        stray = REPO_ROOT / "metadata.py"
        assert not stray.exists(), "unexpected file at repo root; test would be inconclusive"
        # A basename that certainly exists inside the venv/site-packages but is
        # not tracked at the repo root.
        assert audit.resolve_reference("metadata.py") == "services/common/common/metadata.py"
        for candidate in audit.resolve_candidates("metadata.py"):
            assert ".venv" not in candidate and "site-packages" not in candidate

    def test_resolution_is_restricted_to_tracked_paths(self) -> None:
        tracked = set(audit.tracked_files())
        assert tracked, "git ls-files returned nothing -- test environment is not a checkout"
        assert not any(".venv" in path or "site-packages" in path for path in tracked)
        for ref in ("claims.py", "qdrant_filters.py", "REQUIREMENTS.md"):
            resolved = audit.resolve_reference(ref)
            assert resolved in tracked, f"{ref} resolved outside the tracked set: {resolved}"

    def test_an_ambiguous_bare_name_does_not_silently_pick_one(self, monkeypatch) -> None:
        """Several tracked files share the basename, so the citation is a guess.

        The old code took `matches[0]`, which is how a wrong resolution could look
        like a successful one. Reported instead, and it fails the run: the fix is
        for the document to cite the path.
        """
        monkeypatch.setattr(
            audit, "tracked_files", lambda: ("services/a/models.py", "services/b/models.py")
        )
        assert audit.resolve_candidates("models.py") == [
            "services/a/models.py",
            "services/b/models.py",
        ]
        assert audit.resolve_reference("models.py") is None

        text = _mapping("| 1.1 X | see `models.py` | Implemented |")
        row = audit.assess(text, {})["rows"][0]
        assert row["ambiguous_references"] == {
            "models.py": ["services/a/models.py", "services/b/models.py"]
        }
        assert row["broken_references"] == [], "ambiguous is not the same finding as broken"

    def test_an_ambiguous_reference_fails_the_run(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            audit, "tracked_files", lambda: ("services/a/models.py", "services/b/models.py")
        )
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping("| 1.1 X | see `models.py` | Implemented |"), encoding="utf-8")
        assert audit.main(["--mapping", str(mapping), "--ledger", str(tmp_path / "none.md")]) == 1

    def test_a_missing_git_checkout_reports_broken_not_wrong(self, monkeypatch) -> None:
        """Without git, resolution finds nothing rather than guessing from disk.

        A broken reference is a visible finding; a wrong one is not. That is the
        whole lesson of the venv bug, so the degraded path fails safe.
        """
        monkeypatch.setattr(audit, "tracked_files", lambda: ())
        assert audit.resolve_reference("claims.py") is None
        text = _mapping("| 1.1 X | see `claims.py` | Implemented |")
        assert audit.assess(text, {})["rows"][0]["broken_references"] == ["claims.py"]

    @pytest.mark.parametrize("span", ["rag-clearance:*", "--strict", "image:", "status", "8.5"])
    def test_non_path_backticks_are_not_treated_as_references(self, span: str) -> None:
        """Roles, flags and prose live in backticks too; flagging them would make
        the check noisy enough that people stop reading it."""
        assert audit.file_references(f"something `{span}` here") == []


class TestStatusVocabulary:
    @pytest.mark.parametrize(
        "status",
        [
            "Validated live",
            "Partial",
            "Gap (known, tracked)",
            "TBD (organizational)",
            "Implemented (ratification pending owner, issue #519)",
            "Validated live (append-only); Partial (trace coverage)",
            "Privacy validated live; fairness scoped out (documented)",
            "N/A (documented)",
        ],
    )
    def test_documented_statuses_are_accepted(self, status: str) -> None:
        text = _mapping(f"| 1.1 X | evidence | {status} |")
        assert audit.assess(text, {})["rows"][0]["in_vocabulary"]

    def test_invented_status_is_rejected(self) -> None:
        text = _mapping("| 1.1 X | evidence | Mostly fine |")
        assert not audit.assess(text, {})["rows"][0]["in_vocabulary"]

    def test_lowercase_mid_sentence_terms_still_count(self) -> None:
        """Case-insensitive on purpose -- the real document writes "Privacy
        validated live", and flagging that would report prose style as a defect."""
        text = _mapping("| 1.1 X | evidence | mostly implemented, honestly |")
        assert audit.assess(text, {})["rows"][0]["in_vocabulary"]

    def test_compound_status_is_ranked_on_its_weakest_term(self) -> None:
        term, rank = audit.weakest_status("Validated live (append-only); Gap (trace coverage)")
        assert (term, rank) == ("Gap", audit.STATUS_RANK["Gap"])


class TestDiff:
    def _snapshots(self, before: str, after: str) -> dict:
        base = audit.assess(_mapping(before), {})
        current = audit.assess(_mapping(after), {})
        return audit.diff_snapshots(base, current)

    def test_improvement_is_reported_but_not_a_regression(self) -> None:
        delta = self._snapshots("| 1.1 X | e | Gap |", "| 1.1 X | e | Validated live |")
        assert len(delta["status_changes"]) == 1
        assert delta["status_changes"][0]["regression"] is False

    def test_regression_is_flagged(self) -> None:
        delta = self._snapshots("| 1.1 X | e | Validated live |", "| 1.1 X | e | Gap |")
        assert delta["status_changes"][0]["regression"] is True

    def test_unchanged_status_produces_no_entry(self) -> None:
        delta = self._snapshots("| 1.1 X | e | Partial |", "| 1.1 X | e | Partial |")
        assert delta["status_changes"] == []

    def test_added_and_removed_rows_are_listed(self) -> None:
        delta = self._snapshots(
            "| 1.1 X | e | Partial |\n| 1.2 Y | e | Gap |",
            "| 1.1 X | e | Partial |\n| 1.3 Z | e | Gap |",
        )
        assert delta["added"] == ["GOVERN 1.3 Z"]
        assert delta["removed"] == ["GOVERN 1.2 Y"]

    def test_newly_broken_reference_is_distinguished_from_an_already_broken_one(self) -> None:
        """An audit should surface what broke *since last time*, not re-litigate
        a reference that was already known bad."""
        delta = self._snapshots(
            "| 1.1 X | `docs/gone-a.md` | Partial |",
            "| 1.1 X | `docs/gone-a.md` and `docs/gone-b.md` | Partial |",
        )
        assert delta["newly_broken_references"] == [
            {"outcome": "GOVERN 1.1 X", "references": ["docs/gone-b.md"]}
        ]


class TestIssueStates:
    def test_closed_referenced_issue_is_surfaced(self, tmp_path: Path) -> None:
        export = tmp_path / "issues.json"
        export.write_text(json.dumps([{"number": 526, "state": "CLOSED"}]), encoding="utf-8")
        states = audit.load_issue_states(export)
        text = _mapping("| 1.1 X | gap tracked in #526 | Gap |")
        row = audit.assess(text, states)["rows"][0]
        assert row["issues"] == ["526"] and row["closed_issues"] == ["526"]

    def test_no_export_means_issue_state_is_simply_unknown(self) -> None:
        """Offline is the default: NFR-1 forbids depending on a network call, and
        an artifact that varies with GitHub's availability is not evidence."""
        text = _mapping("| 1.1 X | gap tracked in #526 | Gap |")
        row = audit.assess(text, {})["rows"][0]
        assert row["issues"] == ["526"] and row["closed_issues"] == []


class TestExitStatus:
    def _run(self, tmp_path: Path, row: str, *extra: str) -> int:
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping(row), encoding="utf-8")
        return audit.main(
            ["--mapping", str(mapping), "--ledger", str(tmp_path / "absent.md"), *extra]
        )

    def test_clean_mapping_exits_zero(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, "| 1.1 X | `REQUIREMENTS.md` | Validated live |") == 0

    def test_broken_reference_exits_one(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, "| 1.1 X | `docs/nope.md` | Validated live |") == 1

    def test_off_vocabulary_status_exits_one(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, "| 1.1 X | `REQUIREMENTS.md` | Excellent |") == 1

    def test_missing_mapping_exits_two(self, tmp_path: Path) -> None:
        assert audit.main(["--mapping", str(tmp_path / "absent.md")]) == 2

    def test_missing_baseline_exits_two(self, tmp_path: Path) -> None:
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping("| 1.1 X | e | Partial |"), encoding="utf-8")
        code = audit.main(["--mapping", str(mapping), "--baseline", str(tmp_path / "absent.json")])
        assert code == 2

    def test_regression_exits_zero_by_default_and_one_under_strict(self, tmp_path: Path) -> None:
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping("| 1.1 X | e | Validated live |"), encoding="utf-8")
        baseline = tmp_path / "b.json"
        assert audit.main(["--mapping", str(mapping), "--snapshot", str(baseline)]) == 0

        mapping.write_text(_mapping("| 1.1 X | e | Gap |"), encoding="utf-8")
        args = ["--mapping", str(mapping), "--baseline", str(baseline)]
        assert audit.main(args) == 0, "a regression must not fail a normal audit run"
        assert audit.main([*args, "--strict"]) == 1


class TestReport:
    def test_report_records_the_commit_and_leaves_judgement_blank(self, tmp_path: Path) -> None:
        """The generated file must not read as a completed audit: the mechanical
        checks are only half of one, and the signature lines are what make the
        rest visible as missing."""
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping("| 1.1 X | `REQUIREMENTS.md` | Partial |"), encoding="utf-8")
        report = tmp_path / "snap" / "internal-audit.md"
        assert audit.main(["--mapping", str(mapping), "--report", str(report)]) == 0
        body = report.read_text(encoding="utf-8")
        assert "Audited commit:" in body
        assert "Auditor judgement — to be completed by hand" in body
        assert "Auditor signature" in body
        assert "It is not a" in body and "finished audit" in body

    def test_report_lists_broken_references(self, tmp_path: Path) -> None:
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping("| 1.1 X | `docs/nope.md` | Partial |"), encoding="utf-8")
        report = tmp_path / "internal-audit.md"
        audit.main(["--mapping", str(mapping), "--report", str(report)])
        assert "docs/nope.md" in report.read_text(encoding="utf-8")

    def test_report_carries_the_ledger_when_present(self, tmp_path: Path) -> None:
        ledger = tmp_path / "README.md"
        ledger.write_text(
            "## Open decisions ledger\n\n"
            "| # | Decision | Recorded in | Tracking |\n|---|---|---|---|\n"
            "| 12 | Management-review cadence + first review | x | Issue #542 |\n"
            "\n## Next section\n",
            encoding="utf-8",
        )
        mapping = tmp_path / "m.md"
        mapping.write_text(_mapping("| 1.1 X | e | Partial |"), encoding="utf-8")
        report = tmp_path / "internal-audit.md"
        audit.main(["--mapping", str(mapping), "--ledger", str(ledger), "--report", str(report)])
        body = report.read_text(encoding="utf-8")
        assert "Open-decisions ledger at audit time" in body
        assert "Management-review cadence" in body


def test_ledger_parsing_ignores_the_other_tables_in_the_readme() -> None:
    text = (
        "## Open decisions ledger\n\n"
        "| # | Decision | Recorded in | Tracking |\n|---|---|---|---|\n"
        "| 1 | Owner appointment | x | Issue #519 |\n"
        "| 12 | Cadence | y | Issue #542 |\n"
        "\n## Audit evidence package — where each item lives\n\n"
        "| Evidence item | Location | Status |\n|---|---|---|\n"
        "| Something | somewhere | Draft |\n"
    )
    assert [row[0] for row in audit.ledger_rows(text)] == ["1", "12"]


def test_the_real_mapping_still_parses_and_resolves() -> None:
    """Guards the parser against the real document drifting away from it.

    Deliberately asserts only that parsing yields a plausible number of rows and
    that nothing is off-vocabulary -- not the statuses themselves, which change.
    """
    text = (REPO_ROOT / "docs" / "nist-ai-rmf" / "rmf-mapping.md").read_text(encoding="utf-8")
    result = audit.assess(text, {})
    assert len(result["rows"]) > 30, "parser no longer matches the mapping's table shape"
    assert [r["outcome"] for r in result["rows"] if not r["in_vocabulary"]] == []
    assert [r["outcome"] for r in result["rows"] if r["broken_references"]] == []
