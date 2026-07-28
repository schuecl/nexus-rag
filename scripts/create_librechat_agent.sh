#!/usr/bin/env bash
# Create (import) the "RAG Assistant" LibreChat agent from
# infra/librechat/agents/rag-assistant.json.
#
# LibreChat has no file-based agent import in v0.8.7, so agents are created
# through its authenticated REST API. This script does what a browser would:
# mints a LibreChat session JWT for an existing user (signed with the same
# JWT_SECRET the librechat container runs with) and POSTs the agent definition.
# Agents are per-author, so pass the username of a user who has already logged
# in at least once (their record must exist in LibreChat's Mongo).
#
# Prereqids: the stack is up (`docker compose up`), the target user has logged
# into LibreChat once via Keycloak, and the rag MCP tool has been "Connect"ed by
# that user in the UI (per-user OAuth -- see docs/querying-the-corpus.md).
#
# Usage: scripts/create_librechat_agent.sh [username]   (default: dave-admin)
set -euo pipefail

USER_NAME="${1:-dave-admin}"
COMPOSE="${COMPOSE:-docker compose}"
LC_URL="${LC_URL:-https://localhost:3080}"
AGENT_JSON="$(dirname "$0")/../infra/librechat/agents/rag-assistant.json"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

lc()    { $COMPOSE exec -T librechat "$@"; }
mongo() { $COMPOSE exec -T mongodb mongosh LibreChat --quiet --eval "$1"; }

echo "Looking up LibreChat user '$USER_NAME'..."
# .toString() is deliberate: some mongosh versions print a bare ObjectId as
# `ObjectId('...')` rather than the hex string alone, which then gets minted
# straight into the JWT's "id" claim and fails LibreChat's `Cast to ObjectId`
# lookup with an opaque 500 (issue #97, found live).
UID_HEX="$(mongo "print((db.users.findOne({username:'$USER_NAME'})||{})._id.toString())" | tr -d '[:space:]')"
[ -n "$UID_HEX" ] && [ "$UID_HEX" != "undefined" ] || {
  echo "ERROR: user '$USER_NAME' not found in LibreChat Mongo -- log in via Keycloak once first." >&2; exit 1; }

JWT_SECRET="$(lc printenv JWT_SECRET | tr -d '[:space:]')"
[ -n "$JWT_SECRET" ] || { echo "ERROR: JWT_SECRET not set in the librechat container." >&2; exit 1; }

TOKEN="$(python3 - "$UID_HEX" "$JWT_SECRET" <<'PY'
import sys, hmac, hashlib, base64, json, time
uid, secret = sys.argv[1], sys.argv[2]
b64 = lambda x: base64.urlsafe_b64encode(x).rstrip(b'=')
now = int(time.time())
h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(',', ':')).encode())
p = b64(json.dumps({"id": uid, "iat": now, "exp": now + 600}, separators=(',', ':')).encode())
sig = b64(hmac.new(secret.encode(), h + b'.' + p, hashlib.sha256).digest())
print((h + b'.' + p + b'.' + sig).decode())
PY
)"

echo "Creating agent as '$USER_NAME'..."
# -A sets a browser User-Agent: LibreChat's uaParser middleware rejects non-browser
# requests with "Illegal request".
HTTP="$(curl -sk -o /tmp/agent_resp.json -w '%{http_code}' -X POST "$LC_URL/api/agents" \
  -A "$UA" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @"$AGENT_JSON")"

if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
  python3 -c "import json;d=json.load(open('/tmp/agent_resp.json'));print('Created agent:', d.get('id'), '-', d.get('name'))"
else
  echo "ERROR: HTTP $HTTP"; cat /tmp/agent_resp.json; exit 1
fi
echo "Done. Select 'RAG Assistant' under the Agents endpoint in LibreChat."
