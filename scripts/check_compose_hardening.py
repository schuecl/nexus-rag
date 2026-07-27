#!/usr/bin/env python3
"""Issue #111 guard: keep the Compose stack's container hardening from drifting
away from the Helm chart's securityContext, and keep internal services off the
host's external interfaces.

The Compose stack is not a toy here -- `docs/dev-setup.md` is the primary
verification story and `e2e.yml`'s golden-query job runs against it. When it
diverges from the chart, the thing everyone actually exercises is the weaker
one, and settings like `read_only` only fail at deploy time instead of in dev.
So this is enforced mechanically, the same way NFR-16 image pinning is, rather
than left to review.

Three rules:

1. The four custom-built services must carry the Compose equivalents of
   `nexus-rag.podSecurityContext` / `nexus-rag.containerSecurityContext`:
   `user: "10001:10001"`, `read_only: true`, `cap_drop: [ALL]`, and
   `security_opt: [no-new-privileges:true]`.
2. Every other long-running service must at least set `no-new-privileges`.
   `cap_drop: [ALL]` is not required of them -- postgres and keycloak both drop
   privileges from root at startup and need CAP_CHOWN/SETUID/SETGID to do it.
3. Every published port must bind an explicit host address. A bare "8003:8003"
   listens on all interfaces; on a laptop on a shared network that is an open
   unauthenticated model-inference endpoint.

Exits 1 and prints every violation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

# Built here rather than pulled, so this repo controls the Dockerfile and the
# uid it declares -- these get the full chart-equivalent treatment.
CUSTOM_SERVICES = {
    "ingestion-api",
    "ingestion-worker",
    "orchestration-mcp",
    "reranker-service",
}

# One-shot init/seed/eval containers: they run to completion under `run --rm`
# or a profile, hold no listening port, and several need to write as root
# (ollama-model-init pulls into the model volume). Exempt from rule 2 rather
# than silently passing it.
ONE_SHOT = {
    "ollama-model-init",
    "seed-sample-data",
    "eval-retrieval",
    "harden-audit-log",
}

NO_NEW_PRIVILEGES = "no-new-privileges:true"

# Services that legitimately keep their default capability set because they
# drop privileges from root at startup, or take ownership of a volume on first
# boot, and need CAP_CHOWN/SETUID/SETGID/DAC_OVERRIDE to do it. Listed by name
# so each exemption is a visible decision rather than an absent check --
# every one of these was found by the container failing to start, not by
# reading the image.
CAPABILITY_EXEMPT = {
    "postgres",
    "keycloak",
    "alloy",
    "librechat",
    "librechat-proxy",
    "litellm",
    "mongodb",
    "syslog-collector",
    "provision-metrics-view",
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _check_service(name: str, spec: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    security_opt = _as_list(spec.get("security_opt"))
    cap_drop = _as_list(spec.get("cap_drop"))

    if name in CUSTOM_SERVICES:
        if spec.get("user") != "10001:10001":
            problems.append(
                f'{name}: user must be "10001:10001" (the uid its Dockerfile '
                f"declares, matching the chart's runAsUser), got {spec.get('user')!r}"
            )
        if spec.get("read_only") is not True:
            problems.append(
                f"{name}: read_only must be true (the chart sets "
                f"readOnlyRootFilesystem); add a tmpfs for any path it writes"
            )
        if spec.get("read_only") is True and not spec.get("tmpfs"):
            problems.append(f"{name}: read_only with no tmpfs -- /tmp will not be writable")
        if "ALL" not in cap_drop:
            problems.append(f"{name}: cap_drop must include ALL (the chart drops all)")

    if name not in ONE_SHOT and NO_NEW_PRIVILEGES not in security_opt:
        problems.append(f"{name}: security_opt must include {NO_NEW_PRIVILEGES!r}")

    for published in _as_list(spec.get("ports")):
        # "8003:8003" binds every interface; "127.0.0.1:8003:8003" does not.
        # A long-form mapping (a dict) is handled by the isinstance check in
        # the caller -- only short-form strings reach here.
        if published.count(":") < 2:
            problems.append(
                f"{name}: port {published!r} binds all interfaces; "
                f"prefix it with an explicit host address (127.0.0.1:)"
            )
    return problems


def main() -> int:
    compose = yaml.safe_load(COMPOSE.read_text())
    services: dict[str, Any] = compose.get("services", {})

    missing = CUSTOM_SERVICES - services.keys()
    if missing:
        print(f"{COMPOSE.name}: expected custom services are absent: {sorted(missing)}")
        return 1

    problems: list[str] = []
    for name, spec in sorted(services.items()):
        if not isinstance(spec, dict):
            continue
        ports = spec.get("ports")
        if isinstance(ports, list) and any(isinstance(p, dict) for p in ports):
            # Long-form ports set host_ip explicitly or not at all; checking
            # the short form covers everything this file currently uses, and
            # this makes the gap loud instead of a silent pass.
            problems.append(f"{name}: long-form ports are not checked by this guard yet")
        problems.extend(_check_service(name, spec))

    if problems:
        print(f"{COMPOSE.name}: container hardening violations (issue #111):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("Compose services match the chart's hardening; no port binds all interfaces.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
