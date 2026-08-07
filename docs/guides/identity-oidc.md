# Identity & OIDC: dev realm to air-gapped

Every access decision in this system — what a user may tag at upload, what a
curator may approve, what a query may return — is derived server-side from
**verified OIDC claims**. That makes the Keycloak realm configuration a
security surface, not just plumbing: get the claims wrong and the mandatory
retrieval filter is built from wrong inputs; get the client URLs wrong and
logins break or, worse, tokens become stealable.

This page documents the identity contract, reviews exactly which parts of the
dev realm survive a move to new hostnames and which must change, and walks the
promotion path: dev sandbox → connected staging → air-gapped production, for
both Compose and Helm, with a copy-paste migration script using dummy values.

## 1. The claims contract

Services never trust anything the client sends about identity. Tokens are
verified (RS256 signature against the issuer's JWKS, `iss` against an
allowlist, `aud` must contain `rag-app`), then five claims drive every
decision:

| Claim | Where it lives in Keycloak | Example value | What it controls |
|---|---|---|---|
| `rag_roles` | **Client roles** on the `rag-app` client | `["rag-query", "rag-clearance:SECRET", "rag-releasability:NATO"]` | Everything role-shaped — see vocabulary below |
| `groups` | User **attribute** `groups` (multivalued) | `["USAREUR-AF", "Signal-Corps"]` | Access-scope matching: a document tagged with an `access_scope` is only retrievable by users holding that group |
| `org` | User **attribute** `org` (single value) | `USAREUR-AF` | Organizational ownership (which org's curators review your uploads) |
| `aud` | `audience-rag-app` protocol mapper | `rag-app` | Token audience check — a token minted for another client is rejected |
| `sub` / `preferred_username` | Standard OIDC | — | Audit-log identity |

The `rag_roles` claim is a **flat list of client roles** with a prefix
convention — clearance and releasability ride inside it rather than being
separate claims:

| Role pattern | Meaning |
|---|---|
| `rag-ingest` | May upload documents |
| `rag-query` | May run retrieval |
| `rag-admin` | May administer classification/releasability vocabularies |
| `rag-purge` | May execute purge (erasure) operations |
| `rag-curate:<org>` | May curate (approve/reject) documents owned by `<org>` |
| `rag-clearance:<level>` | Single ranked clearance level, e.g. `rag-clearance:SECRET` |
| `rag-releasability:<value>` | One role per releasability marking the user may see, e.g. `rag-releasability:FVEY` |

All of this is delivered by one dedicated **client scope**,
`nexus-rag-claims`, attached as a default scope to both clients (`rag-app`
and `librechat`). Its four protocol mappers are the entire mechanism:
`groups-attr` and `org` map the user attributes, `rag_roles` maps the
`rag-app` client roles, and `audience-rag-app` stamps the audience.

!!! tip "Why this design survives migration"
    Nothing in the claims model references a hostname, port, or URL. Users,
    attributes, client roles, and the `nexus-rag-claims` scope port to any
    environment **verbatim**. When you move the stack behind new URLs, only
    the *client* and *server* configuration changes — never the
    roles/groups/claims themselves. That is the boundary this page is
    organized around.

## 2. What must change when URLs change — and what must not

Reviewing the dev realm (`infra/keycloak/realm-export/nexus-rag-realm.json`)
against a hostname move, the URL-coupled surfaces are exactly five:

| # | Surface | Dev value | What production needs |
|---|---|---|---|
| 1 | `rag-app` client `redirectUris` / `webOrigins` | `["*"]` (wildcard) | Explicit HTTPS callback URLs only — the ingestion UI's `/auth/callback` on its real hostname |
| 2 | `librechat` client `redirectUris` / `webOrigins` | `https://localhost:3080/...` | The real chat hostname's `/oauth/openid/callback` |
| 3 | Keycloak's own hostname (`KC_HOSTNAME`) | unset (`start-dev`) | The canonical issuer hostname — it is baked into every minted token's `iss` |
| 4 | Services' issuer allowlist (`OIDC_ISSUERS` / `externalKeycloak.issuerUrl`) | comma-separated list of dev hostnames | One canonical `https://keycloak.internal.example.mil/realms/nexus-rag` |
| 5 | Realm `sslRequired` | `none` | `external` (or `all`) — never `none` outside a laptop |

Everything else — the five seeded personas, their attributes, the client-role
vocabulary, the `nexus-rag-claims` scope and its mappers — is
hostname-agnostic and needs no edit to *work*. (Seeded personas need to be
**removed** for production, but that's a hygiene change, not a URL change —
covered below.)

!!! danger "The two dev-realm settings you must never ship"
    - **`redirectUris: ["*"]`** — a wildcard redirect URI means any site can
      receive your users' authorization codes. It exists in the dev realm so
      the sandbox works on any port without editing the realm. Production
      registers explicit URLs, nothing else.
    - **`sslRequired: none`** — allows the entire OIDC exchange over
      plaintext HTTP. Same rationale, same rule: dev-only.

## 3. How the services consume identity

Both deployment shapes feed the same four knobs into the services
(`common/claims.py` is the single shared implementation):

=== "Docker Compose"

    ```bash title=".env (connected staging example)"
    # Comma-separated allowlist -- a token's `iss` must match one entry.
    # The FIRST entry is also where JWKS signing keys are fetched from,
    # so it must be reachable from inside the services' network.
    OIDC_ISSUERS=https://keycloak.staging.example.com/realms/nexus-rag

    OIDC_AUDIENCE=rag-app
    OIDC_CLIENT_ID=rag-app
    RAG_APP_KEYCLOAK_CLIENT_SECRET=<from your secret store>

    # Must exactly match a redirectUri registered on the rag-app client
    OIDC_REDIRECT_URI=https://rag.staging.example.com/auth/callback
    ```

=== "Helm"

    ```yaml title="values override (production example)"
    externalKeycloak:
      # Single canonical issuer in production. Comma-separated lists are
      # supported but exist for the dev two-hostnames-one-Keycloak case --
      # a real deployment has one hostname and needs one entry.
      issuerUrl: "https://keycloak.internal.example.mil/realms/nexus-rag"
      audience: "rag-app"
      clientId: "rag-app"
      clientSecret:
        existingSecret: "nexus-rag-keycloak-client-secret"
        secretKey: "client-secret"

    ingestionApi:
      oidcRedirectUri: "https://rag.internal.example.mil/auth/callback"
    ```

    ```bash title="the referenced Secret, created out-of-band"
    kubectl create secret generic nexus-rag-keycloak-client-secret \
      --from-literal=client-secret="$(cat /path/to/real-secret)"
    ```

Two behaviors worth knowing before you debug a 401:

- **JWKS comes from the first issuer.** With multiple `OIDC_ISSUERS`
  entries, signature keys are fetched from
  `<first entry>/protocol/openid-connect/certs`. A token whose `iss` matches
  the *second* entry still verifies — same Keycloak, same keys — but if the
  first entry is unreachable *from the service pods*, every login fails even
  though the second hostname works from your browser.
- **The issuer string must match byte-for-byte.** Keycloak mints `iss` from
  its configured hostname (`KC_HOSTNAME`). If Keycloak believes it is
  `https://sso.internal.example.mil` and your allowlist says
  `https://keycloak.internal.example.mil`, verification fails with a correct
  signature. Set `KC_HOSTNAME` first, then copy the resulting issuer into
  the services.

## 4. Migrating the realm: connected → air-gapped

The dev realm export is the *starting template*, not the production realm.
The promotion transform does five things: pin redirect URIs, enforce TLS,
strip the seeded personas, drop the dev client secrets, and keep everything
claims-related untouched. Using dummy air-gapped hostnames
(`*.internal.example.mil` — replace with your enclave's):

```bash title="migrate-realm.sh -- transform the dev export into a production realm import"
#!/bin/bash
# Usage: ./migrate-realm.sh <dev-realm.json> <out.json>
# Produces a realm import for the air-gapped Keycloak. Claims machinery
# (client scopes, mappers, role vocabulary) passes through unchanged.
set -euo pipefail

RAG_UI_URL="https://rag.internal.example.mil"          # ingestion UI/API
CHAT_URL="https://chat.internal.example.mil"           # LibreChat

jq \
  --arg rag_cb "$RAG_UI_URL/auth/callback" \
  --arg rag_origin "$RAG_UI_URL" \
  --arg chat_cb "$CHAT_URL/oauth/openid/callback" \
  --arg chat_origin "$CHAT_URL" \
  '
  # 1. TLS is mandatory outside the sandbox
  .sslRequired = "external"

  # 2. Explicit redirect URIs -- no wildcards in production, ever
  | (.clients[] | select(.clientId == "rag-app")) |=
      (.redirectUris = [$rag_cb] | .webOrigins = [$rag_origin])
  | (.clients[] | select(.clientId == "librechat")) |=
      (.redirectUris = [$chat_cb] | .webOrigins = [$chat_origin])

  # 3. Dev client secrets never travel -- Keycloak generates new ones on
  #    import; read them out afterwards and place them in your secret store
  | .clients[].secret? = null | del(.clients[].secret | nulls)

  # 4. Seeded dev personas (devpass123) never travel either. Real users
  #    arrive by federation (LDAP/AD) or are provisioned by your IdM --
  #    what they need is the same attributes + client roles the personas
  #    model (see the provisioning template below).
  | .users = []
  ' "$1" > "$2"

echo "Wrote $2 -- import with:"
echo "  /opt/keycloak/bin/kc.sh start --import-realm  (file mounted at /opt/keycloak/data/import/)"
echo "Then read the regenerated client secrets:"
echo "  kcadm.sh get clients -r nexus-rag --fields clientId,secret"
```

After import, two manual steps complete the cut-over:

```bash title="read the regenerated rag-app secret into your secret store"
# on the Keycloak host / pod
/opt/keycloak/bin/kcadm.sh config credentials \
  --server https://keycloak.internal.example.mil --realm master \
  --user "$KC_ADMIN_USER"
CLIENT_UUID=$(/opt/keycloak/bin/kcadm.sh get clients -r nexus-rag \
  -q clientId=rag-app --fields id --format csv --noquotes)
/opt/keycloak/bin/kcadm.sh get "clients/$CLIENT_UUID/client-secret" -r nexus-rag
# -> place `value` in the platform secret store; it feeds
#    RAG_APP_KEYCLOAK_CLIENT_SECRET (Compose) or the
#    nexus-rag-keycloak-client-secret Secret (Helm)
```

??? example "Provisioning template: what every real user needs"
    Whether users come from LDAP federation or IdM-driven provisioning, each
    one needs the same three things the dev personas model. As `kcadm.sh`
    (scriptable against an existing Keycloak, no realm re-import):

    ```bash
    U=someuser
    kcadm.sh create users -r nexus-rag -s username=$U -s enabled=true \
      -s 'attributes.org=["USAREUR-AF"]' \
      -s 'attributes.groups=["USAREUR-AF","Signal-Corps"]'

    # roles: base capability + clearance + one role per releasability value
    kcadm.sh add-roles -r nexus-rag --uusername $U --cclientid rag-app \
      --rolename rag-query \
      --rolename rag-clearance:SECRET \
      --rolename rag-releasability:FVEY --rolename rag-releasability:NATO
    ```

    A user with **no** `rag-clearance:*` role is treated as unclassified-only
    by design (fail-closed) — mis-provisioning degrades access, never
    escalates it. In a DoD enclave the browser-facing login is typically
    CAC/PIV: Keycloak's X.509 authentication flow handles the certificate;
    the claims contract above is unchanged by *how* authentication happens.

## 5. Air-gapped specifics

Beyond the realm transform, four enclave realities to plan for — these are
the ones that bite after the wire is cut:

1. **DNS and reachability.** The canonical issuer hostname must resolve and
   route *from inside the cluster/stack network*, not just from admin
   workstations — the services fetch JWKS from it at runtime (first-issuer
   rule above). On Kubernetes, if Keycloak lives *outside* the cluster,
   verify egress to it is allowed; the chart deliberately leaves egress
   unrestricted so your platform policy (e.g. NSX DFW on Tanzu) is the
   arbiter.
2. **Private CA trust.** An air-gapped Keycloak serves TLS from your
   enclave CA. The service containers must trust that CA or every JWKS
   fetch fails at the TLS layer: mount the CA bundle and point
   `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` at it (Compose: a bind-mounted
   file + environment; Helm: a ConfigMap volume added via your values).
   The same applies to LibreChat's `OPENID_ISSUER` connection.
3. **Clock sync.** Token validation checks `exp`/`iat`. Enclave NTP (or
   chrony against a local time source) on every node — clock skew between
   Keycloak and the services produces intermittent, maddening 401s.
4. **No public IdP fallbacks.** Everything above assumes Keycloak is
   *inside* the enclave. Realm import happens at deploy time from the
   transformed export; no outbound identity dependency exists at runtime.

## 6. Verify the cut-over

Same probes as the sandbox walkthroughs, pointed at the new hostnames. Mint
a token with a real (or staging) account and inspect exactly the claims the
services will act on:

```bash
KC=https://keycloak.internal.example.mil
TOKEN=$(curl -s "$KC/realms/nexus-rag/protocol/openid-connect/token" \
  -d grant_type=password -d client_id=rag-app \
  -d client_secret="$RAG_APP_SECRET" \
  -d username="$TEST_USER" -d password="$TEST_PASS" | jq -r .access_token)

# decode the payload (verification is the servers' job -- this is a look, not a check)
echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null \
  | jq '{iss, aud, org, groups, rag_roles}'
```

Checklist — each maps to a failure mode above:

- [ ] `iss` equals the configured `OIDC_ISSUERS` / `issuerUrl` value exactly
- [ ] `aud` contains `rag-app`
- [ ] `rag_roles` carries the expected `rag-clearance:*` / `rag-releasability:*` entries
- [ ] `groups` / `org` attributes present
- [ ] A retrieval probe (`POST /debug/rag_search` with the token) returns
      results with `applied_filter` reflecting those claims
- [ ] The ingestion UI completes the browser login round-trip on the real
      hostname (proves redirect URI registration + `KC_HOSTNAME` agree)

## Where each knob lives — quick index

| Setting | Compose | Helm |
|---|---|---|
| Issuer allowlist | `OIDC_ISSUERS` in `.env` | `externalKeycloak.issuerUrl` |
| Audience / client id | `OIDC_AUDIENCE` / `OIDC_CLIENT_ID` | `externalKeycloak.audience` / `.clientId` |
| Client secret | `RAG_APP_KEYCLOAK_CLIENT_SECRET` | Secret `nexus-rag-keycloak-client-secret`, key `client-secret` |
| UI redirect URI | `OIDC_REDIRECT_URI` | `ingestionApi.oidcRedirectUri` |
| Realm content | `infra/keycloak/realm-export/` auto-imported | your transformed export, imported into the enclave Keycloak |

## Sources

- [`infra/keycloak/realm-export/nexus-rag-realm.json`](https://github.com/schuecl/nexus-rag/blob/main/infra/keycloak/realm-export/nexus-rag-realm.json)
  — the dev realm this page's review and migration script start from
- [`services/common/common/claims.py`](https://github.com/schuecl/nexus-rag/blob/main/services/common/common/claims.py)
  — the single shared claims-verification implementation (issuer allowlist,
  first-issuer JWKS, role-prefix parsing, fail-closed clearance)
- [`helm/nexus-rag/values.yaml`](https://github.com/schuecl/nexus-rag/blob/main/helm/nexus-rag/values.yaml)
  — the `externalKeycloak` values contract
- [Keycloak server administration guide](https://www.keycloak.org/docs/latest/server_admin/)
  — realm import/export, client scopes, X.509 authentication flows
