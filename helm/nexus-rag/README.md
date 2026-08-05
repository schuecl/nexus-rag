# nexus-rag Helm chart (NFR-10)

Production packaging for the air-gapped Kubernetes deployment. Scoped to only
the components this project adds: **ingestion-api**, **ingestion-worker**,
**orchestration-mcp**, **reranker-service**, a dedicated **embedding-service**
(Ollama, embedding model only), **Qdrant**, and **NATS** (JetStream -- the
durable ingestion job queue, NFR-11). LibreChat, LiteLLM, Keycloak, and the cluster's existing
generation-serving vLLM/Ollama (C7) are assumed to already be deployed and
managed separately; this chart integrates with them via configuration
(`externalKeycloak`, MCP server registration in LibreChat's own config)
rather than deploying or bundling them.

**Assumption called out explicitly:** REQUIREMENTS.md's NFR-10 lists Qdrant
among the new components this chart deploys, but doesn't mention Postgres —
unlike Docker Compose's dev stack (which stands up its own Postgres from
scratch, per NFR-9), this chart treats Postgres as existing cluster
infrastructure too, connected to via a pre-created Secret rather than
deployed by the chart. If that's wrong for your environment, `values.yaml`'s
`externalPostgres` section is the place to revisit.

`helm lint helm/nexus-rag` and `helm template helm/nexus-rag --debug` run in
CI on every PR (`.github/workflows/security.yml`'s `helm` job), against the
default values plus the one override (`ingestionApi.oidcRedirectUri`) the
chart fails closed on without an ingress host configured. That job does not
exercise every value combination this chart supports (e.g. `vectorBackend:
milvus`, or any of the `external` blocks below) — render locally with the
combination you're about to deploy before trusting it.

## Prerequisites

- A Kubernetes cluster with a default `StorageClass` (or set
  `*.persistence.storageClassName` explicitly for each component)
- The air-gapped registry (`global.imageRegistry`) already has this
  project's four custom images (`ingestion-api`, `ingestion-worker`,
  `orchestration-mcp`, `reranker-service`) mirrored into it (NFR-1), plus
  `qdrant/qdrant`, `milvus`, `ollama/ollama`, and/or `nats` for whichever of
  those this chart is self-deploying — skip mirroring the ones you've
  pointed at an `external.host` instead
- A pre-created Secret matching `externalPostgres.existingSecret` /
  `externalPostgres.secretKey`, containing a full SQLAlchemy
  `DATABASE_URL` (`postgresql+psycopg://user:pass@host:5432/dbname`)
- Keycloak realm/client already configured per REQUIREMENTS.md Section 6.2
  (see `infra/keycloak/realm-export/` for the dev-stack equivalent to adapt)
- A pre-created Secret matching `externalKeycloak.clientSecret.existingSecret` /
  `.secretKey`, containing the `rag-app` client's confidential-client secret —
  needed for the ingestion UI's browser OIDC login (ARCHITECTURE.md Section
  4.4: the auth-code exchange and token refresh are server-to-server calls
  against Keycloak's token endpoint)
- A pre-created Secret matching `ingestionApi.sessionTokenEncryption.existingSecret` /
  `.secretKey`, containing a Fernet key (32 url-safe base64-encoded bytes) —
  encrypts the OIDC access/refresh/id tokens `ingestion-api` stores server-side
  for browser sessions at rest (`common/token_crypto.py`), so a read-only
  compromise of the app database alone doesn't yield usable Keycloak
  credentials (issue #213). Generate one with `python3 -c "from
  cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Either `ingestionApi.ingress.enabled: true` with `ingestionApi.ingress.host`
  set, or an explicit `ingestionApi.oidcRedirectUri` — the chart fails the
  render otherwise, rather than silently deploying a broken OIDC login
  callback URL (`_helpers.tpl`'s `nexus-rag.oidcRedirectUri`)
- A pre-created Secret matching `qdrant.apiKey.existingSecret`, containing two
  keys: `qdrant.apiKey.secretKey` (a full read/write API key) and
  `qdrant.apiKey.readOnlySecretKey` (a read-only one) — Qdrant requires
  authenticated access in every environment (NFR-15); `ingestion-worker` and
  `ingestion-api` both get the full key (the worker creates the collection
  and writes new points; ingestion-api updates/deletes points on
  approve/reject/supersede), `orchestration-mcp` gets the read-only one.
  Generate both however your cluster's secret-management practice calls for
  (e.g. `openssl rand -hex 32` for each) before creating the Secret.
- An S3-compatible bucket (existing enterprise S3, Ceph RGW, ...) already
  reachable at `externalObjectStore.endpoint`/`.bucket`, and a pre-created
  Secret matching `externalObjectStore.existingSecret` containing an access
  key (`.accessKeySecretKey`) and secret key (`.secretKeySecretKey`) with
  read/write access to it — original uploaded files are stored there,
  independent of Qdrant/Postgres (NFR-12)
- Two pre-created Secrets, matching `nats.credentials.ingestionApi` and
  `.ingestionWorker` (`.existingSecret`/`.secretKey` each) — `ingestion-api`
  (publisher) and `ingestion-worker` (consumer) each authenticate to NATS
  with their own password, restricted to publish-only/consume-only by
  `templates/nats-configmap.yaml`'s per-subject permissions (NFR-11, issue
  #212)
- Optionally, a pre-created Secret matching
  `rerankerService.sharedSecret.existingSecret` / `.secretKey` — see
  [reranker-service shared secret](#reranker-service-shared-secret-issue-216)
  below. Left empty by default, so the chart renders and runs without it,
  same as before that issue.

## External backing services

Every non-custom backing service this chart touches now supports the same
choice: deploy it (default) or connect to one that already exists.
`externalPostgres`, `externalKeycloak`, and `externalObjectStore` have always
been connect-only — this chart doesn't deploy Postgres, Keycloak, or an
object store at all (see the assumption called out above). `qdrant`,
`milvus`, `nats`, and `embeddingService` now support both:

| Component | Self-deploy (default) | Connect to existing |
|---|---|---|
| Qdrant | `qdrant.enabled: true` | `qdrant.enabled: false` + `qdrant.external.host` |
| Milvus | `milvus.enabled: true` | `milvus.enabled: false` + `milvus.external.host` |
| NATS | `nats.enabled: true` | `nats.enabled: false` + `nats.external.host` |
| Embedding | `embeddingService.enabled: true` | `embeddingService.enabled: false` + `embeddingService.external.host` |

Each `external` block also takes `.port` (defaults match the self-deployed
Service port) and `.tls` (`false` by default; `true` selects `https://` for
Qdrant/Milvus/the embedding service, `tls://` for NATS). The existing
credential values still apply in external mode exactly as in self-deploy
mode — `qdrant.apiKey`, `milvus.auth`, and `nats.credentials` all just point
at a pre-created Secret, and nothing about that changes when the endpoint
behind it is someone else's cluster instead of this chart's own
StatefulSet. Setting `enabled: false` without the matching `external.host`
fails the render with a specific message (`_helpers.tpl`'s
`nexus-rag.qdrantUrl`/`milvusUrl`/`natsUrl`/`embeddingUrl`) rather than
silently deploying a broken URL, same pattern as `oidcRedirectUri` below.

One thing `external` mode does **not** change: `networkPolicy`'s ingress
rules for that component stop rendering entirely — they select this chart's
own pod, which no longer exists. Protecting an external instance's ingress
is that cluster's own concern.

`embeddingService.external.apiCompatibility` selects which wire protocol the
endpoint speaks: `"ollama"` (default) is Ollama's native
`/api/embeddings`, unauthenticated — what the self-deployed instance
(`enabled: true`) always speaks, and what any Ollama-*compatible* external
endpoint speaks too. `"openai"` targets an OpenAI-API-compliant hosted model
instead (vLLM, TGI, a cloud embedding endpoint) via `/v1/embeddings`,
authenticated with a bearer token from
`embeddingService.external.apiKey.existingSecret` — same `existingSecret`
pattern as everywhere else in this chart, and, like the reranker-service
shared secret above, only sent as a header when that Secret is actually
configured. `common/embedding_client.py` is the shared client both
`ingestion-worker` and `orchestration-mcp` use for this.

Also note this same instance serves
`ingestionWorker.visionModel`/`classificationModel`/`piiLlmModel` when those
are set (still Ollama-native calls, `common/embedding_client.py` doesn't
cover those) — an external endpoint needs every model this deployment
actually uses provisioned on it, not just `embeddingService.model`.

## Network policy (issue #110)

`networkPolicy.enabled` defaults to **true**. Qdrant, NATS, the embedding
service, and reranker-service then accept traffic only from the components
that legitimately call them.

This matters more than the usual defence-in-depth argument. `orchestration-mcp`
is the only place FR-26 is enforced — it derives the access filter from the
caller's claims and hands it to Qdrant, which has no notion of clearance and
applies whatever filter it is given. Chunk payloads also hold the source text
in cleartext, so anything that can reach Qdrant reads the whole corpus at every
classification level without inverting an embedding. `reranker-service` is
sharper still: it receives full chunk text, so this NetworkPolicy was, until
issue #216, the *only* control standing between an unauthorized caller and
that content — see the next section for the credential now available in
addition to it.

**Two values must be set or things will not work**, deliberately left empty
rather than guessed:

| Value | Consequence if unset |
|---|---|
| `networkPolicy.ingressControllerSelectors` | `ingestion-api` denies **all** ingress — the UI is unreachable |
| `networkPolicy.mcpClients` | LibreChat's MCP calls are dropped; the ingestion UI's own `/search` page still works, so this can look healthy when it is not |

A plausible-but-wrong default (say, assuming `ingress-nginx`) would silently
grant access to the wrong namespace while appearing configured, which is worse
than an obvious outage. `helm install` prints both warnings.

`networkPolicy.denyEgressByDefault` is **off** by default: every custom service
needs the external Postgres and Keycloak, two also need the external object
store, and any of Qdrant/Milvus/NATS/the embedding service running in
`external` mode adds another address this chart doesn't know either. Turning
it on without populating `networkPolicy.egressAllow` will break the
deployment.

## reranker-service shared secret (issue #216)

`/rerank` receives full retrieved chunk text — post-access-filter content
already cleared for a specific caller, per FR-26 — and, before this issue, had
no way to check who was asking beyond whatever the NetworkPolicy above let
through. That's a real gap on a CNI that doesn't enforce policy at all.

`rerankerService.sharedSecret.existingSecret` / `.secretKey` point at a
pre-created Secret holding one value, read by both sides: `orchestration-mcp`
sends it as an `X-Reranker-Shared-Secret` header, and `reranker-service`
rejects any `/rerank` call that doesn't present the matching value (401).
Deliberately **not** the caller's own OIDC token forwarded downstream —
FR-26's enforcement already happened in `orchestration-mcp` before this hop,
and re-verifying it in `reranker-service` too would duplicate that logic in a
second place it can drift out of sync, for no additional access-control
benefit (see the issue's discussion of that heavier alternative).

Left empty by default: the chart renders and runs exactly as it did before
this issue, and `reranker-service` logs a startup warning when it comes up
without the secret configured, so that omission is visible in the pod logs
rather than silent. Generate one (e.g. `openssl rand -hex 32`) and set it in
any deployment where the NetworkPolicy might not be the only thing standing
between an unauthorized caller and retrieved chunk content — which, given the
CNI caveat above, is any deployment that hasn't specifically verified
otherwise.

Finally: a NetworkPolicy is inert unless the cluster's CNI enforces it
(Calico, Cilium, and Antrea do; some managed CNIs silently do not). Kubernetes
reports no error either way — verify rather than assume.

## Install

```bash
helm install nexus-rag ./helm/nexus-rag \
  --namespace nexus-rag --create-namespace \
  --set global.imageRegistry=registry.internal.example.mil/nexus-rag \
  --set externalKeycloak.issuerUrl=https://keycloak.example.mil/realms/nexus-rag \
  --set externalPostgres.existingSecret=nexus-rag-db \
  --set ingestionApi.ingress.enabled=true \
  --set ingestionApi.ingress.host=rag-ingest.example.mil
```

Or supply a `values-production.yaml` override file with all of the above
(and image tags pinned to your mirrored versions) rather than a long
`--set` chain.

## What this chart does NOT do

- Deploy or configure LibreChat, LiteLLM, Keycloak, or the generation-serving
  vLLM/Ollama (C7) — confirm those are already reachable before installing.
- Register `orchestration-mcp` as an MCP server with LibreChat — that's a
  LibreChat-side config change (`librechat.yaml`'s `mcpServers`), done
  separately. See `infra/librechat/librechat.yaml` in the repo for the
  dev-stack's version of that config to adapt.
- Grant Keycloak's fine-grained token-exchange admin permission needed for
  the OBO flow (Section 7.7/6.1) — a manual admin-console step against
  Keycloak, not something Helm or the application can do for you.
- Set up PodDisruptionBudgets or HorizontalPodAutoscalers — not included in
  this pass; add them if your cluster's baseline requires them. (NetworkPolicies
  *are* included — see [Network policy](#network-policy-issue-110) above.)
- Harden `qdrant`'s or `embeddingService`'s `securityContext` — both run
  upstream images (`qdrant/qdrant`, `ollama/ollama`) whose own user/filesystem
  conventions this chart doesn't override. `ingestion-api`, `ingestion-worker`,
  `orchestration-mcp`, and `reranker-service` (the four custom-built images)
  *do* run hardened: `services/*/Dockerfile` bakes in a fixed non-root UID/GID
  (10001), and their Deployments set `runAsNonRoot: true`,
  `readOnlyRootFilesystem: true`, and drop all capabilities
  (`nexus-rag.podSecurityContext`/`nexus-rag.containerSecurityContext` in
  `_helpers.tpl`), with `emptyDir` volumes at `/tmp` (upload spooling,
  ML-library scratch files) and, for `ingestion-worker`/`orchestration-mcp`, at
  their `HF_HOME` model cache (no PVC there — see the persistence note below).

## Persistence notes

`reranker-service` and `embedding-service` each mount a single
`ReadWriteOnce` PVC for their model cache. Both default to `replicas: 1`;
scaling either beyond that will fail to schedule concurrently unless your
storage class supports `ReadWriteMany`. Qdrant runs as a single-node
`StatefulSet` — no distributed clustering (multi-node consensus, shard
replication) is configured; REQUIREMENTS.md doesn't call for it, and it's
meaningfully more operational complexity than this chart takes on.

## Encryption at rest (NFR-6)

NFR-6 calls for the vector store and raw document storage to "support
encryption at rest," with MPNexus's existing PyKMIP deployment named as "a
candidate key-management integration point" — not a settled design. Disk/
volume encryption is a **StorageClass** (or underlying block-storage)
property; it isn't something a Helm chart, Qdrant, or this project's
application code can turn on by itself. What this chart does:

- `qdrant.persistence.storageClassName`, `embeddingService.persistence.storageClassName`,
  and `rerankerService.persistence.storageClassName` are all left overridable
  (empty string = your cluster's default StorageClass, whatever that
  provides) — point them at an encrypted StorageClass if your cluster offers
  one, the same way you'd do for any other PVC-backed workload.
- `externalPostgres` is, per this chart's scope, infrastructure you already
  manage separately — its encryption-at-rest posture is entirely that
  deployment's responsibility, not something a `DATABASE_URL` Secret
  reference can configure.

What this chart deliberately does **not** attempt: a concrete PyKMIP
integration. REQUIREMENTS.md itself only names PyKMIP as a candidate, not a
specified integration (what it would encrypt, at what layer, with what key
rotation policy are all still open); building against an unspecified design
would mean guessing at requirements rather than implementing them. Revisit
this section once that design exists.
