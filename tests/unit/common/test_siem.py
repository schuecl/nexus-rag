"""NFR-2 (#73): audit events are exportable to the environment's SIEM.

Covers the RFC 5424 formatting, the severity/msgid rules, the after_insert
hook actually firing on a real session flush (in-memory SQLite), the
disabled-by-default posture, and — the property that matters most in
production — fail-open: a dead collector must never break the write path.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import ssl
import threading
import uuid
from datetime import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlmodel import Session, SQLModel, create_engine

from common import siem
from common.models import AuditLogEntry
from common.siem import enable_siem_export, format_rfc5424


@pytest.fixture
def entry() -> AuditLogEntry:
    return AuditLogEntry(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        actor_sub="sub-1",
        actor_username="alice",
        action="document.submit",
        target_id="doc-1",
        detail={"classification": "SECRET"},
        created_at=datetime(2026, 7, 27, 12, 0, 0),
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def udp_collector():
    """A real UDP socket standing in for the SIEM collector."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2)
    yield sock
    sock.close()


@pytest.fixture(autouse=True)
def _reset_siem_state():
    """enable_siem_export() mutates module state; leave no test residue
    (including the sender's socket -- filterwarnings=error turns an unclosed
    socket into a failure, which is exactly the leak-detection we want)."""
    yield
    if siem._state["sender"] is not None:
        siem._state["sender"].close()
    siem._state["sender"] = None
    siem._state["send_failed_once"] = False


class TestFormat:
    def test_rfc5424_header_and_json_payload(self, entry):
        message = format_rfc5424(entry, "ingestion-api", "host-1", 42).decode("ascii")
        header, payload = message.split(" - ", 1)
        # facility 13 (log audit) * 8 + notice(5) = 109
        assert header.startswith("<109>1 2026-07-27T12:00:00Z host-1 nexus-rag-ingestion-api 42 ")
        assert header.endswith("document.submit")
        parsed = json.loads(payload)
        assert parsed["actor_sub"] == "sub-1"
        assert parsed["action"] == "document.submit"
        assert parsed["target_id"] == "doc-1"
        assert parsed["detail"] == {"classification": "SECRET"}
        assert parsed["service"] == "ingestion-api"

    def test_denied_actions_go_out_at_warning_severity(self, entry):
        entry.action = "query.denied"
        message = format_rfc5424(entry, "orchestration-mcp", "h", 1)
        # facility 13 * 8 + warning(4) = 108
        assert message.startswith(b"<108>1 ")

    def test_control_characters_in_detail_cannot_forge_a_second_record(self, entry):
        entry.detail = {"reason": "x\n<109>1 forged evil record"}
        message = format_rfc5424(entry, "s", "h", 1)
        # ensure_ascii leaves exactly one syslog record: no raw newline survives
        assert b"\n" not in message
        payload = json.loads(message.decode("ascii").split(" - ", 1)[1])
        assert payload["detail"]["reason"] == "x\n<109>1 forged evil record"

    def test_msgid_is_sanitized_to_rfc_limits(self, entry):
        entry.action = "a b" + "c" * 64  # space is forbidden, length capped at 32
        message = format_rfc5424(entry, "s", "h", 1).decode("ascii")
        msgid = message.split(" - ", 1)[0].split(" ")[-1]
        assert " " not in msgid
        assert len(msgid) == 32

    def test_facility_is_env_overridable(self, monkeypatch, entry):
        # A collector routing on local0 (16): PRI = 16*8 + notice(5) = 133.
        monkeypatch.setenv("SIEM_SYSLOG_FACILITY", "16")
        assert format_rfc5424(entry, "s", "h", 1).startswith(b"<133>1 ")

    def test_invalid_facility_falls_back_to_log_audit(self, monkeypatch, entry):
        monkeypatch.setenv("SIEM_SYSLOG_FACILITY", "99")
        assert format_rfc5424(entry, "s", "h", 1).startswith(b"<109>1 ")


class TestExport:
    def test_disabled_without_host(self, monkeypatch):
        monkeypatch.delenv("SIEM_SYSLOG_HOST", raising=False)
        assert enable_siem_export("ingestion-api") is False
        assert siem._state["sender"] is None

    def test_audit_insert_reaches_the_collector(self, monkeypatch, db, udp_collector, entry):
        host, port = udp_collector.getsockname()
        monkeypatch.setenv("SIEM_SYSLOG_HOST", host)
        monkeypatch.setenv("SIEM_SYSLOG_PORT", str(port))
        assert enable_siem_export("ingestion-api") is True

        db.add(entry)
        db.commit()

        datagram, _ = udp_collector.recvfrom(65535)
        payload = json.loads(datagram.decode("ascii").split(" - ", 1)[1])
        assert payload["id"] == "00000000-0000-0000-0000-000000000001"
        assert payload["action"] == "document.submit"

    def test_dead_collector_does_not_break_the_audit_write(self, monkeypatch, db, entry):
        # TCP to a port nothing listens on: connect refused on every send.
        monkeypatch.setenv("SIEM_SYSLOG_HOST", "127.0.0.1")
        monkeypatch.setenv("SIEM_SYSLOG_PORT", "1")  # privileged + unused
        monkeypatch.setenv("SIEM_SYSLOG_PROTOCOL", "tcp")
        assert enable_siem_export("ingestion-api") is True

        db.add(entry)
        db.commit()  # must not raise: fail-open

        stored = db.get(AuditLogEntry, entry.id)
        assert stored is not None  # the durable row is unaffected

    def test_tcp_export_uses_octet_counted_framing(self, monkeypatch, db, entry):
        # RFC 6587: "<len> <msg>" over a stream -- the framing a TCP syslog
        # collector actually parses.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        host, port = server.getsockname()
        monkeypatch.setenv("SIEM_SYSLOG_HOST", host)
        monkeypatch.setenv("SIEM_SYSLOG_PORT", str(port))
        monkeypatch.setenv("SIEM_SYSLOG_PROTOCOL", "tcp")
        try:
            assert enable_siem_export("ingestion-api") is True
            db.add(entry)
            db.commit()
            conn, _ = server.accept()
            conn.settimeout(2)
            data = conn.recv(65535)
            conn.close()
        finally:
            server.close()
        length, _, message = data.partition(b" ")
        assert int(length) == len(message)
        assert json.loads(message.decode("ascii").split(" - ", 1)[1])["action"] == "document.submit"

    def test_enable_twice_sends_once(self, monkeypatch, db, udp_collector, entry):
        host, port = udp_collector.getsockname()
        monkeypatch.setenv("SIEM_SYSLOG_HOST", host)
        monkeypatch.setenv("SIEM_SYSLOG_PORT", str(port))
        enable_siem_export("ingestion-api")
        enable_siem_export("ingestion-api")  # idempotent: no duplicate listener

        db.add(entry)
        db.commit()

        udp_collector.recvfrom(65535)
        with pytest.raises(TimeoutError):
            udp_collector.recvfrom(65535)


def _self_signed_cert(tmp_path):
    """A throwaway CA-less self-signed cert for 127.0.0.1, written to disk the
    way a real deployment would mount one."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "collector.crt"
    key_path = tmp_path / "collector.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _TlsCollector:
    """A real TLS syslog collector: accepts one connection, records one
    octet-counted frame."""

    def __init__(self, cert_path, key_path):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self._context = context
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(3)
        self.received: bytes | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def address(self):
        return self._server.getsockname()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
            with self._context.wrap_socket(conn, server_side=True) as tls:
                tls.settimeout(3)
                self.received = tls.recv(65535)
        except (TimeoutError, ssl.SSLError, OSError):
            pass  # handshake-refused tests land here by design

    def wait(self):
        self._thread.join(timeout=4)
        self._server.close()


class TestTlsExport:
    """RFC 5425: octet-counted syslog inside TLS -- the production transport
    for a collector on a protected segment."""

    def _configure(self, monkeypatch, host, port, **extra):
        monkeypatch.setenv("SIEM_SYSLOG_HOST", host)
        monkeypatch.setenv("SIEM_SYSLOG_PORT", str(port))
        monkeypatch.setenv("SIEM_SYSLOG_PROTOCOL", "tls")
        for k, v in extra.items():
            monkeypatch.setenv(k, v)

    def test_export_over_verified_tls(self, monkeypatch, db, entry, tmp_path):
        cert_path, key_path = _self_signed_cert(tmp_path)
        collector = _TlsCollector(cert_path, key_path)
        host, port = collector.address
        # The collector's own cert acts as the CA bundle -- exactly how a
        # deployment trusts a private-PKI collector via SIEM_SYSLOG_CA_CERT.
        self._configure(monkeypatch, host, port, SIEM_SYSLOG_CA_CERT=str(cert_path))
        assert enable_siem_export("ingestion-api") is True

        db.add(entry)
        db.commit()
        collector.wait()

        assert collector.received is not None
        length, _, message = collector.received.partition(b" ")
        assert int(length) == len(message)
        payload = json.loads(message.decode("ascii").split(" - ", 1)[1])
        assert payload["action"] == "document.submit"

    def test_untrusted_collector_is_refused_but_fails_open(
        self, monkeypatch, db, entry, tmp_path
    ):
        # No CA configured, so the self-signed collector must fail
        # verification -- the export is refused (no plaintext-equivalent
        # fallback), while the audit row still lands (fail-open).
        cert_path, key_path = _self_signed_cert(tmp_path)
        collector = _TlsCollector(cert_path, key_path)
        host, port = collector.address
        self._configure(monkeypatch, host, port)
        assert enable_siem_export("ingestion-api") is True

        db.add(entry)
        db.commit()  # must not raise
        collector.wait()

        assert collector.received is None  # nothing crossed the channel
        assert db.get(AuditLogEntry, entry.id) is not None

    def test_verify_false_accepts_untrusted_cert_for_debugging(
        self, monkeypatch, db, entry, tmp_path
    ):
        cert_path, key_path = _self_signed_cert(tmp_path)
        collector = _TlsCollector(cert_path, key_path)
        host, port = collector.address
        self._configure(monkeypatch, host, port, SIEM_SYSLOG_TLS_VERIFY="false")
        assert enable_siem_export("ingestion-api") is True

        db.add(entry)
        db.commit()
        collector.wait()

        assert collector.received is not None
