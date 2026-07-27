"""Dev-only syslog collector for validating NFR-2's SIEM export (#73).

Stands in for the environment's real SIEM so the export path can be watched
end to end in the Compose stack: every message any service emits via
common/siem.py lands in this container's stdout, tagged with the transport it
arrived on. `docker compose --profile siem-debug up syslog-collector`, point
the services at it (SIEM_SYSLOG_HOST=syslog-collector), and `docker compose
logs -f syslog-collector`.

Listens on all three transports the exporter speaks:

    UDP 514    one RFC 5424 message per datagram
    TCP 514    RFC 6587 octet-counted framing ("<len> <msg>"), with a
               newline-delimited fallback for hand testing via netcat
    TLS 6514   RFC 5425 (octet-counted inside TLS) -- enabled only when the
               cert/key mounted at TLS_CERT/TLS_KEY exist, which the Compose
               service wires to the same dev certs Keycloak already uses
               (infra/certs/generate-dev-certs.sh)

Deliberately stdlib-only and single-file: this is a validation harness, not a
log pipeline. A production deployment points SIEM_SYSLOG_HOST at the real
collector instead of running this at all.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import threading

# Loopback by default (CodeQL py/bind-socket-all-network-interfaces): binding
# every interface is only right inside a container network namespace, where
# "all interfaces" means the container's own veth and traffic is governed by
# what the Compose file publishes. The Compose service sets BIND_HOST=0.0.0.0
# explicitly for that reason; run directly on a host, this stays loopback.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
UDP_PORT = int(os.environ.get("UDP_PORT", "514"))
TCP_PORT = int(os.environ.get("TCP_PORT", "514"))
TLS_PORT = int(os.environ.get("TLS_PORT", "6514"))
TLS_CERT = os.environ.get("TLS_CERT", "/certs/dev.crt")
TLS_KEY = os.environ.get("TLS_KEY", "/certs/dev.key")


def emit(transport: str, message: bytes) -> None:
    text = message.decode("utf-8", errors="replace").rstrip("\n")
    print(f"[{transport}] {text}", flush=True)


def udp_listener() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND_HOST, UDP_PORT))
    emit("udp", f"listening on {BIND_HOST}:{UDP_PORT}".encode())
    while True:
        datagram, _ = sock.recvfrom(65535)
        emit("udp", datagram)


def read_frames(conn: socket.socket, transport: str) -> None:
    """Split a stream into messages: RFC 6587 octet-counted if the frame
    starts with a digit-run + space, newline-delimited otherwise."""
    buffer = b""
    while True:
        chunk = conn.recv(65535)
        if not chunk:
            break
        buffer += chunk
        while buffer:
            if buffer[:1].isdigit():
                length_bytes, sep, rest = buffer.partition(b" ")
                if not sep:
                    break  # incomplete length prefix; wait for more
                length = int(length_bytes)
                if len(rest) < length:
                    break  # incomplete frame; wait for more
                emit(transport, rest[:length])
                buffer = rest[length:]
            else:
                line, sep, rest = buffer.partition(b"\n")
                if not sep:
                    break
                if line:
                    emit(transport, line)
                buffer = rest


def stream_listener(port: int, transport: str, context: ssl.SSLContext | None) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND_HOST, port))
    server.listen(8)
    emit(transport, f"listening on {BIND_HOST}:{port}".encode())
    while True:
        conn, _ = server.accept()
        if context is not None:
            try:
                conn = context.wrap_socket(conn, server_side=True)
            except ssl.SSLError as exc:
                emit(transport, f"TLS handshake failed: {exc}".encode())
                conn.close()
                continue
        threading.Thread(target=read_frames, args=(conn, transport), daemon=True).start()


def main() -> None:
    threads = [
        threading.Thread(target=udp_listener, daemon=True),
        threading.Thread(target=stream_listener, args=(TCP_PORT, "tcp", None), daemon=True),
    ]
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(TLS_CERT, TLS_KEY)
        threads.append(
            threading.Thread(
                target=stream_listener, args=(TLS_PORT, "tls", context), daemon=True
            )
        )
    else:
        emit("tls", f"disabled ({TLS_CERT} not present)".encode())
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
