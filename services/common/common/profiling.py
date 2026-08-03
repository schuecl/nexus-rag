"""Continuous CPU profiling for the four services (#349, #133's Pyroscope
follow-on).

#133 deployed Pyroscope as a stack component the same way Tempo was --
present before any service sends it data, with instrumentation as a
separate, later change (#134 did the same for tracing: Tempo shipped empty
in #169, spans followed after). This module is that later change for
profiling.

Continuous, not triggered: sampling stays on for the life of the process
once enabled, the same posture Prometheus and Tempo already have in this
stack -- a dashboard you can look at after the fact, not something that has
to be armed before the slow request happens.

CPU only. Memory profiling (`mem_enabled`) roughly doubles the agent's
overhead for a question nothing has asked yet; add it if a specific
investigation needs heap data.

application_name is deliberately the same string setup_tracing() uses for
service.name (`nexus-rag-<service>`) -- Pyroscope stores it as the
`service_name` label, which is what Grafana's tracesToProfiles correlation
(infra/observability/grafana/provisioning/datasources/datasources.yml)
matches against the span's `service.name` attribute to jump from a trace to
the flame graph for the same service.

Configuration (all optional -- unset server address means profiling is
disabled and setup_profiling() is a no-op):

    PYROSCOPE_SERVER_ADDRESS  Pyroscope ingest URL (e.g. http://pyroscope:4040);
                              unset = disabled
    PYROSCOPE_SAMPLE_RATE     CPU sample rate in Hz (default 100, the SDK's own default)
"""

from __future__ import annotations

import logging
import os

import pyroscope

logger = logging.getLogger("profiling")

_DEFAULT_SAMPLE_RATE = 100

# Idempotence guard: pyroscope.configure() starts a native background thread
# and can't be meaningfully reconfigured in-process; repeated setup_profiling()
# calls (tests, reloads) must not start a second agent.
_state: dict = {"configured": False, "enabled": False}


def _sample_rate() -> int:
    raw = os.environ.get("PYROSCOPE_SAMPLE_RATE", "").strip()
    if not raw:
        return _DEFAULT_SAMPLE_RATE
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value <= 0:
        logger.warning(
            "PYROSCOPE_SAMPLE_RATE=%r is not a positive integer; using %s",
            raw,
            _DEFAULT_SAMPLE_RATE,
        )
        return _DEFAULT_SAMPLE_RATE
    return value


def setup_profiling(service: str) -> bool:
    """Start continuous CPU profiling for this process; call once at
    service startup, alongside setup_tracing().

    Returns True when profiling is active. Disabled (False) when
    PYROSCOPE_SERVER_ADDRESS is unset -- no agent thread starts, no network
    calls, zero cost (#133: opt-in, dev/tuning aid, not part of the
    delivered system).
    """
    if _state["configured"]:
        return _state["enabled"]
    _state["configured"] = True

    server_address = os.environ.get("PYROSCOPE_SERVER_ADDRESS", "").strip()
    if not server_address:
        logger.info("profiling disabled (PYROSCOPE_SERVER_ADDRESS is not set)")
        _state["enabled"] = False
        return False

    rate = _sample_rate()
    pyroscope.configure(
        application_name=os.environ.get("PYROSCOPE_APPLICATION_NAME", f"nexus-rag-{service}"),
        server_address=server_address,
        sample_rate=rate,
        cpu_enabled=True,
        mem_enabled=False,
    )
    _state["enabled"] = True
    logger.info("profiling enabled: pushing to %s, %sHz CPU sampling (#349)", server_address, rate)
    return True
