#!/usr/bin/env python3
"""Issue #78: enforce the mutation-testing kill-rate gate.

mutmut 3.x reports the full per-status tally only on the progress line of
`mutmut run` (there is no machine-readable summary command), so e2e.yml tees
the run output to a file and this script parses the *last* tally line:

    183/183  🎉 161 🫥 0  ⏰ 0  🤔 0  🙁 22  🔇 0

Score = killed / (everything except skipped). "timeout", "suspicious", and
"no tests" all count against the score on purpose: a mutant nothing covers is
indistinguishable from a survivor as far as suite strength goes, and counting
it as anything else would let coverage rot silently through the gate.

Fails closed: no parseable tally line (mutmut crashed, output format changed,
empty file) is an error, not a pass -- the advisory era of this job hid
"failed to collect stats" behind continue-on-error for weeks (#78).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STATUS_EMOJI = {
    "\U0001f389": "killed",  # 🎉
    "\U0001fae5": "skipped",  # 🫥
    "⏰": "timeout",  # ⏰
    "\U0001f914": "suspicious",  # 🤔
    "\U0001f641": "survived",  # 🙁
    "\U0001f507": "no_tests",  # 🔇
}


def parse_tally(text: str) -> dict[str, int] | None:
    """Extract status counts from the last mutmut progress line in `text`."""
    last = None
    for line in text.splitlines():
        if "\U0001f389" in line:
            last = line
    if last is None:
        return None
    counts: dict[str, int] = {}
    for emoji, name in STATUS_EMOJI.items():
        match = re.search(re.escape(emoji) + r"\s*(\d+)", last)
        if match:
            counts[name] = int(match.group(1))
    return counts or None


def kill_rate(counts: dict[str, int]) -> float:
    denominator = sum(count for name, count in counts.items() if name != "skipped")
    if denominator == 0:
        return 100.0
    return counts.get("killed", 0) / denominator * 100.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_output", type=Path, help="file holding `mutmut run` output")
    parser.add_argument("--min-score", type=float, default=80.0)
    args = parser.parse_args(argv)

    try:
        text = args.run_output.read_text(errors="replace")
    except OSError as exc:
        print(f"FAIL: cannot read {args.run_output}: {exc}")
        return 1

    counts = parse_tally(text)
    if counts is None:
        print(
            "FAIL: no mutmut tally line found -- the run crashed before "
            "producing results, or the output format changed. This gate "
            "fails closed (issue #78)."
        )
        return 1

    score = kill_rate(counts)
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    print(f"mutation score {score:.1f}% ({summary})")
    if score < args.min_score:
        print(f"FAIL: below the {args.min_score:.0f}% gate (issue #78)")
        return 1
    print(f"OK: meets the {args.min_score:.0f}% gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
