#!/usr/bin/env python3
"""Issue #212 guard: keep infra/nats/nats.conf (mounted by docker-compose)
and helm/nexus-rag/files/nats.conf (embedded in the chart's ConfigMap) from
drifting apart. Helm can't reference files outside its own chart directory
(templates/nats-configmap.yaml uses `.Files.Get "files/nats.conf"`), so the
same per-user permission rules exist as two files rather than one -- this
is the same idea as check_compose_hardening.py (issue #111): a duplicated
source of truth is only safe if something keeps the copies identical.

Exits 1 and prints a diff-style message if the two files differ.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

COMPOSE_CONF = REPO_ROOT / "infra/nats/nats.conf"
HELM_CONF = REPO_ROOT / "helm/nexus-rag/files/nats.conf"


def _violation(compose_path: Path, helm_path: Path) -> str | None:
    if compose_path.read_text() != helm_path.read_text():
        return (
            f"{compose_path} and {helm_path} have drifted apart. Copy one over "
            "the other so both NATS deployments enforce the same permissions."
        )
    return None


def main() -> int:
    problem = _violation(COMPOSE_CONF, HELM_CONF)

    if problem:
        print(f"Issue #212 violation: {problem}", file=sys.stderr)
        return 1
    print("infra/nats/nats.conf and helm/nexus-rag/files/nats.conf are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
