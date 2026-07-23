# nexus-rag Helm chart (NFR-10)

Production packaging for the air-gapped Kubernetes deployment. Scoped to only
the components this project adds: **ingestion-api**, **orchestration-mcp**,
**reranker-service**, a dedicated **embedding-service** (Ollama, embedding
model only), and **Qdrant**. LibreChat, LiteLLM, Keycloak, and the cluster's
existing generation-serving vLLM/Ollama (C7) are assumed to already be
deployed and managed separately; this chart integrates with them via
configuration (`externalKeycloak`, MCP server registration in LibreChat's own
config) rather than deploying or bundling them.

**Assumption called out explicitly:** REQUIREMENTS.md's NFR-10 lists Qdrant
among the new components this chart deploys, but doesn't mention Postgres —
unlike Docker Compose's dev stack (which stands up its own Postgres from
scratch, per NFR-9), this chart treats Postgres as existing cluster
infrastructure too, connected to via a pre-created Secret rather than
deployed by the chart. If that's wrong for your environment, `values.yaml`'s
`externalPostgres` section is the place to revisit.

**Not verified against a running cluster or `helm lint`/`helm template`** —
this environment had no network access to install the `helm` CLI itself
(the install script's upstream, `get.helm.sh`, and GitHub release downloads
were both unreachable from this sandbox). Templates were written by hand
against well-established, conservative Helm conventions and each `values.yaml`
file was validated as syntactically valid YAML, but the actual Go-template
rendering has not been exercised. Run `helm lint` and `helm template
--debug` against this chart before a real install, and treat anything that
doesn't render cleanly as a bug to fix, not a surprise.

## Prerequisites

- A Kubernetes cluster with a default `StorageClass` (or set
  `*.persistence.storageClassName` explicitly for each component)
- The air-gapped registry (`global.imageRegistry`) already has this
  project's three custom images (`ingestion-api`, `orchestration-mcp`,
  `reranker-service`), plus `qdrant/qdrant` and `ollama/ollama`, mirrored
  into it (NFR-1)
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
- Either `ingestionApi.ingress.enabled: true` with `ingestionApi.ingress.host`
  set, or an explicit `ingestionApi.oidcRedirectUri` — the chart fails the
  render otherwise, rather than silently deploying a broken OIDC login
  callback URL (`_helpers.tpl`'s `nexus-rag.oidcRedirectUri`)

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
- Set up NetworkPolicies, PodDisruptionBudgets, or HorizontalPodAutoscalers
  — not included in this pass; add them if your cluster's baseline requires
  them.
- Harden `qdrant`'s or `embeddingService`'s `securityContext` — both run
  upstream images (`qdrant/qdrant`, `ollama/ollama`) whose own user/filesystem
  conventions this chart doesn't override. `ingestion-api`,
  `orchestration-mcp`, and `reranker-service` (the three custom-built images)
  *do* run hardened: `services/*/Dockerfile` bakes in a fixed non-root UID/GID
  (10001), and their Deployments set `runAsNonRoot: true`,
  `readOnlyRootFilesystem: true`, and drop all capabilities
  (`nexus-rag.podSecurityContext`/`nexus-rag.containerSecurityContext` in
  `_helpers.tpl`), with `emptyDir` volumes at `/tmp` (upload spooling,
  ML-library scratch files) and, for `ingestion-api`/`orchestration-mcp`, at
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
