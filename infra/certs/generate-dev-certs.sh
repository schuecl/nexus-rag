#!/usr/bin/env bash
# Generates a throwaway local CA + one leaf cert for the dev Compose stack
# (issue #75): Keycloak's own openid-client discovery call refuses non-HTTPS
# issuers, and the browser-facing LibreChat URL needs to actually be HTTPS
# for the OIDC redirect round-trip. A single leaf cert covers both
# "localhost" (LibreChat's TLS proxy) and "keycloak" (Keycloak's HTTPS
# listener, reached by both LibreChat's backend over the Compose network and
# the browser via a /etc/hosts alias -- see docs/dev-setup.md).
#
# Not committed to git (see .gitignore) -- re-run this any time the stack is
# rebuilt from scratch. Never use this CA/cert outside local dev.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

CA_KEY=ca.key
CA_CRT=ca.crt
LEAF_KEY=dev.key
LEAF_CRT=dev.crt
DAYS=3650

if [[ -f "$CA_CRT" && -f "$LEAF_CRT" ]]; then
  echo "Certs already exist in $(pwd) -- delete ca.* / dev.* first to regenerate." >&2
  exit 0
fi

openssl genrsa -out "$CA_KEY" 4096
# keyUsage is required, not cosmetic: Python's ssl module (used by scripts/
# adversarial_injection_probe.py's verify=<ca bundle>, issue #453) rejects a
# CA cert lacking it even though `openssl verify` is more lenient and
# accepts basicConstraints=CA:TRUE alone.
openssl req -x509 -new -nodes -key "$CA_KEY" -sha256 -days "$DAYS" \
  -subj "/CN=nexus-rag dev CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out "$CA_CRT"

openssl genrsa -out "$LEAF_KEY" 2048
openssl req -new -key "$LEAF_KEY" -subj "/CN=localhost" -out dev.csr \
  -addext "subjectAltName=DNS:localhost,DNS:keycloak,IP:127.0.0.1"

cat > dev.ext <<'EOF'
subjectAltName = DNS:localhost,DNS:keycloak,IP:127.0.0.1
extendedKeyUsage = serverAuth
EOF

openssl x509 -req -in dev.csr -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$LEAF_CRT" -days "$DAYS" -sha256 -extfile dev.ext

rm -f dev.csr dev.ext

chmod 644 "$CA_CRT" "$LEAF_CRT"
chmod 600 "$CA_KEY" "$LEAF_KEY"

echo "Generated $(pwd)/{$CA_CRT,$LEAF_CRT} (SANs: localhost, keycloak, 127.0.0.1)"
echo "Next: trust $CA_CRT in your browser/system (see docs/dev-setup.md), then docker compose up."
