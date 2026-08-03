"""Structured, level-configurable process logging for every service (#73).

Before this module existed no service configured logging at all, so the root
logger's defaults applied: WARNING to stderr, bare messages. Every
``logger.info(...)`` across the codebase -- consumer liveness, SIEM export
status, provenance notices -- was being silently dropped, and there was no way
to turn on DEBUG for a misbehaving pod without editing code.

One function, called once at service startup:

    setup_logging("ingestion-api")

Configuration via environment, per-container:

    LOG_LEVEL   DEBUG | INFO | WARNING | ERROR | CRITICAL   (default INFO)
    LOG_FORMAT  text | json                                 (default text)

``text`` is the human-readable form for `docker compose logs` and `kubectl
logs`. ``json`` emits one JSON object per line -- the shape log collectors
(and #73's SIEM pipeline, if process logs are shipped alongside audit events)
ingest without a parsing rule.

Both formats are log-injection safe: text escapes control characters in the
rendered message via common/log_safety.py's rule, and json's ``ensure_ascii``
does the equivalent structurally. A hostile value in a claim or filename
cannot forge a second log line in either mode.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

from common.log_safety import log_safe

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# Attribute stamped on handlers this module installed, so repeated calls
# (tests, reloads) reconfigure instead of stacking duplicate handlers.
_MARKER = "_nexus_rag_logging"


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")


def _trace_context() -> dict[str, str]:
    """#133: the ``trace_id``/``span_id`` Grafana's provisioned Loki datasource
    needs to link a log line to its trace in Tempo. Without these the
    trace-to-logs and logs-to-trace buttons that datasource enables have
    nothing to match on, so the correlation the observability stack is for
    silently does not work.

    Returns an empty dict rather than null ids when tracing is off (#134
    leaves it off unless OTEL_EXPORTER_OTLP_ENDPOINT is set), so a log line
    from an untraced process doesn't carry a field of all zeroes that looks
    like a real, permanently-broken link. opentelemetry-api is a dependency of
    every service that calls setup_logging, but the import is guarded anyway:
    a missing tracing package must degrade logging, not disable it.
    """
    try:
        from opentelemetry import trace

        from common.tracing import current_trace_id
    except ImportError:  # pragma: no cover - opentelemetry is always installed
        return {}
    trace_id = current_trace_id()
    if trace_id is None:
        return {}
    # Hex, zero-padded to the width the OTLP/Tempo convention uses -- Grafana
    # matches on the literal string, so `format(id, "x")` would fail to link
    # any id with a leading zero.
    span_id = trace.get_current_span().get_span_context().span_id
    return {"trace_id": trace_id, "span_id": format(span_id, "016x")}


class _TextFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        message = log_safe(record.getMessage())
        line = f"{_timestamp(record)} {record.levelname} [{self._service}] {record.name}: {message}"
        if record.exc_info:
            # Tracebacks are multi-line by design; they come from this process,
            # not from external input, so they are not escaped into one line.
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_trace_context())
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, default=str)


def setup_logging(service: str) -> logging.Logger:
    """Configure the root logger for `service` from LOG_LEVEL / LOG_FORMAT.

    Returns the service's namespace logger as a convenience. Idempotent:
    calling again replaces this module's handler rather than adding another,
    so nothing is ever double-logged.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    if level_name not in _VALID_LEVELS:
        # Fall back rather than crash: a typo in a deployment manifest should
        # cost log verbosity, not the pod.
        fallback = "INFO"
        logging.getLogger("logging-setup").warning(
            "LOG_LEVEL=%r is not one of %s; using %s",
            level_name,
            sorted(_VALID_LEVELS),
            fallback,
        )
        level_name = fallback

    format_name = os.environ.get("LOG_FORMAT", "text").strip().lower()
    formatter: logging.Formatter
    if format_name == "json":
        formatter = _JsonFormatter(service)
    else:
        if format_name != "text":
            logging.getLogger("logging-setup").warning(
                "LOG_FORMAT=%r is not 'text' or 'json'; using text", format_name
            )
        formatter = _TextFormatter(service)

    root = logging.getLogger()
    root.setLevel(level_name)
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, _MARKER, True)
    root.addHandler(handler)
    return logging.getLogger(service)
