#!/usr/bin/env python3
"""Issue #295 guard: every place the stack's version is written down must agree.

The release process (docs/releasing.md) versions the whole stack in lockstep:
one X.Y.Z shared by the Helm chart's `version` AND `appVersion`, all five
service packages' pyproject versions, and the chart's four first-party image
tags. Files are the source of truth -- the vX.Y.Z git tag must match them, not
the other way around (release.yml enforces that half at tag time; this script
enforces file-to-file agreement on every PR, so drift can't land on main and
then surprise the next release).

Checked locations:
  - helm/nexus-rag/Chart.yaml            -> version, appVersion
  - services/*/pyproject.toml            -> [project].version (all five)
  - helm/nexus-rag/values.yaml           -> the four first-party image tags
                                            (ingestion-api, ingestion-worker,
                                            orchestration-mcp, reranker-service)

Optionally pass an expected version (release.yml passes the tag, stripped of
its `v` prefix) to additionally assert everything equals that value.

Exits 1 and prints every disagreeing location if any are found.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICES = [
    "common",
    "ingestion-api",
    "ingestion-worker",
    "orchestration-mcp",
    "reranker-service",
]

# The chart's first-party components, as their values.yaml keys.
CHART_IMAGE_COMPONENTS = {
    "ingestionApi": "ingestion-api",
    "ingestionWorker": "ingestion-worker",
    "orchestrationMcp": "orchestration-mcp",
    "rerankerService": "reranker-service",
}

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def collect_versions() -> dict[str, str]:
    """Map of human-readable location -> version string found there."""
    found: dict[str, str] = {}

    chart = yaml.safe_load((REPO_ROOT / "helm/nexus-rag/Chart.yaml").read_text())
    found["helm/nexus-rag/Chart.yaml version"] = str(chart["version"])
    found["helm/nexus-rag/Chart.yaml appVersion"] = str(chart["appVersion"])

    for service in SERVICES:
        path = REPO_ROOT / "services" / service / "pyproject.toml"
        with path.open("rb") as fh:
            project = tomllib.load(fh)
        found[f"services/{service}/pyproject.toml"] = project["project"]["version"]

    values = yaml.safe_load((REPO_ROOT / "helm/nexus-rag/values.yaml").read_text())
    for key, name in CHART_IMAGE_COMPONENTS.items():
        found[f"helm/nexus-rag/values.yaml {key}.image.tag"] = str(values[key]["image"]["tag"])
        # The tag must belong to the component it pins -- a copy-paste of a
        # fully-qualified foreign repository here would silently deploy the
        # wrong image at the right version.
        repository = values[key]["image"]["repository"]
        if repository != name:
            print(
                f"helm/nexus-rag/values.yaml: {key}.image.repository is "
                f"{repository!r}, expected {name!r} (bare name behind "
                "global.imageRegistry -- see docs/releasing.md)"
            )
            raise SystemExit(1)

    return found


def main() -> int:
    expected = sys.argv[1] if len(sys.argv) > 1 else None
    found = collect_versions()

    problems: list[str] = []
    versions = set(found.values())
    if len(versions) > 1:
        for location, version in sorted(found.items()):
            problems.append(f"{location}: {version}")
        problems.insert(0, "stack version is not consistent across the repo:")

    canonical = next(iter(versions))
    if not SEMVER.match(canonical):
        problems.append(f"version {canonical!r} is not plain X.Y.Z semver")

    if expected is not None and versions == {canonical} and canonical != expected:
        problems.append(
            f"expected version {expected!r} (from the release tag) but the "
            f"repo says {canonical!r} -- the release PR must bump the files "
            "BEFORE the tag is pushed (docs/releasing.md)"
        )

    if problems:
        print("\n".join(problems))
        return 1

    print(f"version consistency: all {len(found)} locations agree on {canonical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
