#!/usr/bin/env python3
"""NFR-16 guard: fail if any container image reference in this repo uses a
floating tag. "Floating" here means: no tag at all, `:latest` / `:*-latest`,
or a bare major version (`:16`, `:3`) that silently tracks every future
minor/patch release.

Checked files: every `image:` line in docker-compose*.yml and every
`FROM` line in Dockerfiles. Stage names (`FROM x AS builder`) and
variable-referencing tags (`${VAR:-default}` falls back to the default,
which is checked as-is) are handled.

Minor-level pins like `python:3.11-slim` still move on patch releases -- the
existing Dockerfiles use them deliberately; tightening those to digests is a
separate hardening decision. This check draws the line at tags that move by
whole minors (bare majors) or arbitrarily (`latest`).

Exits 1 and prints every violation if any are found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

IMAGE_LINE = re.compile(r"^\s*image:\s*(?P<ref>\S+)")
FROM_LINE = re.compile(r"^\s*FROM\s+(?P<ref>\S+)", re.IGNORECASE)

# A tag that is just digits (":16", ":3") floats across every minor/patch.
BARE_MAJOR = re.compile(r"^\d+$")


def _violations(ref: str, origin: str) -> str | None:
    if ref.startswith("${") or "$" in ref.split(":")[0]:
        return None  # fully variable reference; nothing static to check
    # Resolve ${VAR:-default} tags to their default for checking.
    ref = re.sub(r"\$\{[^}]*:-([^}]*)\}", r"\1", ref)
    if "@" in ref:  # digest-pinned: immutable, always fine
        return None
    if ":" not in ref:
        return f"{origin}: {ref!r} has no tag (implicit :latest)"
    tag = ref.rsplit(":", 1)[1]
    if tag == "latest" or tag.endswith("-latest"):
        return f"{origin}: {ref!r} uses a floating 'latest' tag"
    if BARE_MAJOR.match(tag):
        return f"{origin}: {ref!r} is pinned to a bare major version {tag!r}"
    return None


def main() -> int:
    problems: list[str] = []

    for compose_file in sorted(REPO_ROOT.glob("docker-compose*.yml")) + sorted(
        REPO_ROOT.glob("docker-compose*.yaml")
    ):
        for lineno, line in enumerate(compose_file.read_text().splitlines(), 1):
            match = IMAGE_LINE.match(line)
            if match:
                problem = _violations(
                    match.group("ref"), f"{compose_file.name}:{lineno}"
                )
                if problem:
                    problems.append(problem)

    for dockerfile in sorted(REPO_ROOT.rglob("Dockerfile*")):
        if any(part.startswith(".") for part in dockerfile.parts):
            continue
        for lineno, line in enumerate(dockerfile.read_text().splitlines(), 1):
            match = FROM_LINE.match(line)
            if match:
                problem = _violations(
                    match.group("ref"), f"{dockerfile.relative_to(REPO_ROOT)}:{lineno}"
                )
                if problem:
                    problems.append(problem)

    if problems:
        print("NFR-16 violation: floating image tags found:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("All image references are pinned (no latest / bare-major / untagged).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
