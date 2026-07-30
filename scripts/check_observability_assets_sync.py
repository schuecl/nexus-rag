#!/usr/bin/env python3
"""Issue #257 guard: keep the observability assets that exist as two copies
from drifting apart.

Helm cannot reference files outside its own chart directory, so the Grafana
dashboards and the Prometheus alert rules exist twice -- once under
infra/observability/ (mounted by docker-compose) and once under
helm/observability/ (vendored so the chart is self-contained and an operator
can import the dashboards straight from a checkout). This is the same
arrangement issue #212 uses for nats.conf, and it carries the same risk: a
duplicated source of truth is only safe if something keeps the copies
identical.

The failure this prevents is quiet. A panel fixed in the Compose copy but not
the chart copy means the air-gapped Grafana imports the old, broken dashboard,
and nothing anywhere reports a problem -- the operator just sees a panel that
does not work and has no reason to suspect the file is stale.

Exits 1 and names every file that differs, is missing, or is unexpectedly
present on only one side.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (compose-side directory, chart-side directory, glob) pairs that must match.
MIRRORED = [
    (
        REPO_ROOT / "infra/observability/grafana/dashboards",
        REPO_ROOT / "helm/observability/dashboards",
        "*",
    ),
    (
        REPO_ROOT / "infra/observability/prometheus/rules",
        REPO_ROOT / "helm/observability/files/rules",
        "*.yml",
    ),
]


def _violations(source: Path, mirror: Path, pattern: str) -> list[str]:
    problems: list[str] = []

    if not source.is_dir():
        return [f"{source} does not exist -- the guard's own paths are stale."]
    if not mirror.is_dir():
        return [f"{mirror} does not exist -- the chart is missing its vendored copy."]

    source_files = {p.name for p in sorted(source.glob(pattern)) if p.is_file()}
    mirror_files = {p.name for p in sorted(mirror.glob(pattern)) if p.is_file()}

    for name in sorted(source_files - mirror_files):
        problems.append(
            f"{source / name} has no counterpart in {mirror}. "
            f"Copy it over so the chart ships the same asset the Compose stack uses."
        )

    for name in sorted(mirror_files - source_files):
        problems.append(
            f"{mirror / name} exists only in the chart. "
            f"Either add it to {source} or delete it -- an asset that is only in "
            f"the chart is never exercised by the Compose stack or the e2e job."
        )

    for name in sorted(source_files & mirror_files):
        if (source / name).read_bytes() != (mirror / name).read_bytes():
            problems.append(
                f"{source / name} and {mirror / name} have drifted apart. "
                f"Copy one over the other so both deployments carry the same asset."
            )

    return problems


def main() -> int:
    problems: list[str] = []
    for source, mirror, pattern in MIRRORED:
        problems.extend(_violations(source, mirror, pattern))

    if problems:
        for problem in problems:
            print(f"Issue #257 violation: {problem}", file=sys.stderr)
        return 1

    print("infra/observability and helm/observability vendored assets are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
