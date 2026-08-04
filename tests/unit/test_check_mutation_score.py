"""Issue #78: the mutation-score gate must read mutmut's tally correctly and
fail closed when there is nothing to read -- the advisory era of the job spent
weeks green while printing "failed to collect stats", so the failure modes are
the point, not an afterthought.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_mutation_score import kill_rate, main, parse_tally

TALLY = "⠦ 183/183  🎉 161 🫥 0  ⏰ 0  🤔 0  🙁 22  🔇 0  🧙 0"


class TestParseTally:
    def test_reads_every_status_from_the_last_tally_line(self):
        text = "⠋ Running stats\n⠴ 10/183  🎉 5 🫥 0  ⏰ 0  🤔 0  🙁 5  🔇 0\n" + TALLY
        assert parse_tally(text) == {
            "killed": 161,
            "skipped": 0,
            "timeout": 0,
            "suspicious": 0,
            "survived": 22,
            "no_tests": 0,
        }

    def test_no_tally_line_is_none(self):
        assert parse_tally("failed to collect stats. runner returned 1") is None
        assert parse_tally("") is None

    def test_unknown_emoji_are_ignored(self):
        # mutmut 3.6 appends a 🧙 counter the score doesn't use; parsing must
        # not depend on the exact emoji set staying fixed.
        counts = parse_tally(TALLY)
        assert counts is not None
        assert "🧙" not in counts


class TestKillRate:
    def test_skipped_is_excluded_from_the_denominator(self):
        assert kill_rate({"killed": 8, "survived": 2, "skipped": 90}) == 80.0

    def test_timeout_suspicious_and_no_tests_count_against(self):
        # A mutant nothing covers is a survivor as far as suite strength goes.
        assert kill_rate({"killed": 8, "timeout": 1, "suspicious": 1, "no_tests": 2}) == (
            8 / 12 * 100
        )

    def test_nothing_to_run_is_a_pass_not_a_zero_division(self):
        assert kill_rate({"skipped": 5}) == 100.0


class TestMainGate:
    def _write(self, tmp_path, text):
        f = tmp_path / "run.txt"
        f.write_text(text)
        return str(f)

    def test_passes_at_or_above_threshold(self, tmp_path, capsys):
        assert main([self._write(tmp_path, TALLY), "--min-score", "80"]) == 0
        assert "88.0%" in capsys.readouterr().out

    def test_fails_below_threshold(self, tmp_path):
        assert main([self._write(tmp_path, TALLY), "--min-score", "90"]) == 1

    def test_fails_closed_on_missing_tally(self, tmp_path, capsys):
        assert main([self._write(tmp_path, "failed to collect stats")]) == 1
        assert "fails closed" in capsys.readouterr().out

    def test_fails_closed_on_missing_file(self, tmp_path):
        assert main([str(tmp_path / "absent.txt")]) == 1
