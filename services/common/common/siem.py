"""NFR-2: export high-value events to the environment's existing SIEM.

REQUIREMENTS.md:93 says audit-relevant events "should be exportable to the
environment's existing SIEM"; issue #73 found the clause had no implementation
and no gap-list entry. This module is the implementation: every AuditLogEntry
row -- FR-31 already funnels each ingestion, curation, retrieval, and purge
event through that one model, from every service -- is forwarded as an
RFC 5424 syslog message with a JSON payload, the lingua franca of the SIEMs an
air-gapped DoD environment actually runs (Splunk, Elastic, ArcSight, QRadar
all ingest it natively).

Design decisions, and why:

- Hooked as a SQLAlchemy ``after_insert`` mapper event on AuditLogEntry rather
  than a wrapper every call site must remember to use. FR-31 writes happen at
  eleven call sites across four services today; a listener catches all of
  them, and any future one, with zero call-site discipline. The DB row remains
  the durable system of record -- the syslog copy is an export, not a second
  source of truth.

- ``after_insert`` fires during flush, before commit. If the surrounding
  transaction rolls back, the event has still been exported. That is the right
  failure direction for a security audit trail: a SIEM seeing an event that
  didn't durably land is noise; a SIEM missing an event that did land is a
  blind spot.

- Fail-open, never fail-closed: a SIEM outage must not take retrieval or
  ingestion down with it. Send errors are swallowed after a single WARNING
  (then demoted to DEBUG so an extended outage doesn't flood the very logs
  it's failing to forward). The durable audit row is unaffected either way.

- The payload is JSON in the syslog MSG field with ``ensure_ascii`` -- every
  control character arrives escaped, so a hostile value inside ``detail``
  cannot forge a second syslog record (the same log-injection rule
  common/log_safety.py enforces for process logs).

Configuration (all optional -- unset host means the export is disabled). The
collector is whatever the environment already runs: any IP/hostname, any
port, any of the three transports -- nothing here assumes a co-located
sidecar.

    SIEM_SYSLOG_HOST         hostname/IP of the syslog collector; unset = off
    SIEM_SYSLOG_PORT         collector port (default 514; 6514 is the
                             RFC 5425 convention for TLS)
    SIEM_SYSLOG_PROTOCOL     "udp" (default), "tcp" (RFC 6587 octet-counted),
                             or "tls" (RFC 5425: the same octet-counted
                             framing inside TLS)
    SIEM_SYSLOG_FACILITY     RFC 5424 facility number 0..23 (default 13,
                             "log audit") for collectors that route by a
                             different facility, e.g. a localN slot
    SIEM_SYSLOG_CA_CERT      tls only: path to the CA bundle that signed the
                             collector's certificate; unset = the system
                             trust store
    SIEM_SYSLOG_CLIENT_CERT  tls only, optional: client certificate for
    SIEM_SYSLOG_CLIENT_KEY   mutual TLS, if the collector demands it
    SIEM_SYSLOG_TLS_VERIFY   tls only: "false" disables server-certificate
                             verification. Dev/debug escape hatch ONLY -- it
                             is logged loudly, because an unverified TLS
                             channel to a SIEM invites exactly the
                             man-in-the-middle an audit trail must resist.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
from datetime import UTC

from sqlalchemy import event

from common.models import AuditLogEntry

logger = logging.getLogger("siem")

# RFC 5424 facility 13: "log audit" -- the facility defined for exactly this
# kind of record. The default, not a hardcode: SIEM ingest pipelines commonly
# route/filter by facility, so environments whose collector expects a localN
# slot (16-23) can override via SIEM_SYSLOG_FACILITY without a code change.
_FACILITY_LOG_AUDIT = 13
_SEVERITY_NOTICE = 5
_SEVERITY_WARNING = 4


def _facility() -> int:
    raw = os.environ.get("SIEM_SYSLOG_FACILITY", "").strip()
    if not raw:
        return _FACILITY_LOG_AUDIT
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not 0 <= value <= 23:  # RFC 5424's facility range
        logger.warning(
            "SIEM_SYSLOG_FACILITY=%r is not an integer in 0..23; using %d (log audit)",
            raw,
            _FACILITY_LOG_AUDIT,
        )
        return _FACILITY_LOG_AUDIT
    return value

_NILVALUE = "-"

# Module-level so enable_siem_export() is idempotent per process: the mapper
# listener must not be registered twice (each registration would forward every
# event again), and tests need to swap the sender.
_state: dict = {"registered": False, "sender": None, "send_failed_once": False}


def _severity(action: str) -> int:
    """Denials are the events a SIEM alert actually keys on -- someone tried
    to reach something they weren't allowed to -- so they go out at WARNING;
    everything else is NOTICE (normal but significant, per RFC 5424)."""
    return _SEVERITY_WARNING if "denied" in action else _SEVERITY_NOTICE


def _msgid(action: str) -> str:
    """MSGID per RFC 5424: printable US-ASCII, no spaces, max 32 chars.
    Actions are dotted identifiers ("document.submit", "query.denied") which
    already qualify; this guards the constraint rather than trusting it."""
    cleaned = "".join(c for c in action if 33 <= ord(c) <= 126)
    return cleaned[:32] or _NILVALUE


def format_rfc5424(entry: AuditLogEntry, service: str, hostname: str, procid: int) -> bytes:
    """Render one audit row as an RFC 5424 syslog message.

    Header carries the routing/triage fields (severity, timestamp, origin
    service, action as MSGID); the MSG field carries the full event as JSON so
    the SIEM ingests the same payload the audit_log row holds -- actor
    identity, action, target, and the structured detail dict (which, per
    #125/#128, already excludes raw query text).
    """
    pri = _facility() * 8 + _severity(entry.action)
    # RFC 5424 wants an RFC 3339 timestamp with an explicit offset. Normalize
    # instead of assuming: models._utcnow() returns an aware datetime today,
    # but rows written before that change (or by tests) can be naive UTC --
    # blindly appending "Z" to an aware isoformat() produced the invalid
    # "+00:00Z" double offset the live collector run caught.
    created = entry.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    timestamp = created.isoformat().replace("+00:00", "Z")
    payload = json.dumps(
        {
            "id": str(entry.id),
            "service": service,
            "actor_sub": entry.actor_sub,
            "actor_username": entry.actor_username,
            "action": entry.action,
            "target_id": entry.target_id,
            "detail": entry.detail,
            "created_at": timestamp,
        },
        ensure_ascii=True,  # control chars arrive escaped: no forged records
        default=str,
    )
    header = (
        f"<{pri}>1 {timestamp} {hostname} nexus-rag-{service} {procid} "
        f"{_msgid(entry.action)} {_NILVALUE} "
    )
    return header.encode("ascii") + payload.encode("ascii")


class _UdpSender:
    def __init__(self, host: str, port: int) -> None:
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, message: bytes) -> None:
        self._sock.sendto(message, self._addr)

    def close(self) -> None:
        self._sock.close()


class _TcpSender:
    """RFC 6587 octet-counted framing over a lazily-(re)connected socket.
    One reconnect attempt per send; anything beyond that is the caller's
    fail-open handling."""

    def __init__(self, host: str, port: int) -> None:
        self._addr = (host, port)
        self._sock: socket.socket | None = None

    def _wrap(self, sock: socket.socket) -> socket.socket:
        """Transport hook: the TLS subclass wraps here; plain TCP passes
        through."""
        return sock

    def _connect(self) -> socket.socket:
        sock = self._wrap(socket.create_connection(self._addr, timeout=5))
        self._sock = sock
        return sock

    def send(self, message: bytes) -> None:
        framed = str(len(message)).encode("ascii") + b" " + message
        sock = self._sock or self._connect()
        try:
            sock.sendall(framed)
        except OSError:
            # Collector restarted and the kept-alive socket is dead: reconnect
            # once and retry, else let the error propagate to the fail-open
            # wrapper.
            self._sock = None
            self._connect().sendall(framed)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


class _TlsSender(_TcpSender):
    """RFC 5425: syslog over TLS -- the same octet-counted framing as the TCP
    transport, inside a verified TLS session. This is the transport for a
    production collector on a protected segment: the audit stream crosses the
    network encrypted, the collector's identity is verified against a CA, and
    mutual TLS is supported for collectors that require the sender to present
    a certificate too."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ca_cert: str | None,
        client_cert: str | None,
        client_key: str | None,
        verify: bool,
    ) -> None:
        super().__init__(host, port)
        self._server_hostname = host
        context = ssl.create_default_context(cafile=ca_cert or None)
        if not verify:
            # Deliberately loud: an unverified TLS channel to a SIEM accepts
            # any endpoint that answers, which defeats the point of moving off
            # plaintext. Allowed for dev/debug only.
            logger.warning(
                "SIEM_SYSLOG_TLS_VERIFY=false: collector certificate is NOT "
                "verified -- do not run production this way"
            )
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if client_cert:
            context.load_cert_chain(client_cert, client_key or None)
        self._context = context

    def _wrap(self, sock: socket.socket) -> socket.socket:
        return self._context.wrap_socket(sock, server_hostname=self._server_hostname)


def _build_sender() -> _UdpSender | _TcpSender | None:
    host = os.environ.get("SIEM_SYSLOG_HOST", "").strip()
    if not host:
        return None
    protocol = os.environ.get("SIEM_SYSLOG_PROTOCOL", "udp").strip().lower()
    default_port = "6514" if protocol == "tls" else "514"
    port = int(os.environ.get("SIEM_SYSLOG_PORT", default_port))
    if protocol == "tls":
        return _TlsSender(
            host,
            port,
            ca_cert=os.environ.get("SIEM_SYSLOG_CA_CERT", "").strip() or None,
            client_cert=os.environ.get("SIEM_SYSLOG_CLIENT_CERT", "").strip() or None,
            client_key=os.environ.get("SIEM_SYSLOG_CLIENT_KEY", "").strip() or None,
            verify=os.environ.get("SIEM_SYSLOG_TLS_VERIFY", "true").strip().lower()
            != "false",
        )
    if protocol == "tcp":
        return _TcpSender(host, port)
    if protocol != "udp":
        logger.warning(
            "SIEM_SYSLOG_PROTOCOL=%r is not udp, tcp, or tls; defaulting to udp", protocol
        )
    return _UdpSender(host, port)


def _forward(mapper, connection, target: AuditLogEntry) -> None:
    """SQLAlchemy after_insert hook; mapper/connection are part of the event
    signature and deliberately unused."""
    sender = _state["sender"]
    if sender is None:
        return
    try:
        sender.send(
            format_rfc5424(target, _state["service"], _state["hostname"], _state["procid"])
        )
    except Exception:
        # Fail-open (see module docstring): the audit row is already written;
        # a SIEM outage must not break the request. Warn once, then stay quiet
        # at DEBUG so the outage doesn't flood the process logs.
        if not _state["send_failed_once"]:
            _state["send_failed_once"] = True
            logger.warning("SIEM syslog export failed; audit rows are unaffected", exc_info=True)
        else:
            logger.debug("SIEM syslog export still failing", exc_info=True)


def enable_siem_export(service: str) -> bool:
    """Enable NFR-2 SIEM export for this process; call once at service startup.

    Returns True if export is active, False if disabled (no SIEM_SYSLOG_HOST).
    Idempotent: repeated calls refresh the configuration without registering
    the listener twice.
    """
    previous = _state["sender"]
    if previous is not None:
        # Reconfiguration replaces the sender; close the old socket rather
        # than leaking it to the garbage collector.
        previous.close()
    sender = _build_sender()
    _state["sender"] = sender
    _state["service"] = service
    _state["hostname"] = socket.gethostname()
    _state["procid"] = os.getpid()
    _state["send_failed_once"] = False
    if sender is None:
        logger.info("SIEM export disabled (SIEM_SYSLOG_HOST is not set)")
        return False
    if not _state["registered"]:
        event.listen(AuditLogEntry, "after_insert", _forward)
        _state["registered"] = True
    logger.info(
        "SIEM export enabled: forwarding audit events as RFC 5424 syslog (NFR-2)"
    )
    return True
