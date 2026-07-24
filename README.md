# nexus-rag

Enterprise-grade Retrieval-Augmented Generation (RAG) pipeline for **MPNexus** — an
air-gapped DoD Kubernetes environment already running LibreChat, LiteLLM, Keycloak, and
vLLM/Ollama. This project adds document ingestion, mandatory classification/releasability
tagging, curator review, and claims-based access-controlled retrieval on top of that
existing stack, exposed to LibreChat as a custom MCP tool.

Full requirements, design constraints, and open questions: **[REQUIREMENTS.md](REQUIREMENTS.md)**.
For how the pieces fit together — component diagram, data model, and sequence diagrams for
every major flow — see **[ARCHITECTURE.md](ARCHITECTURE.md)**.
Everything below is a snapshot of what's actually built against that spec, not a plan.

## Status

The full ingest → tag → curate → retrieve flow works end to end against every functional
requirement in `REQUIREMENTS.md` (FR-1 through FR-32), with claims-based access control
enforced server-side at every stage — tagging, curation, and retrieval all check the same
OIDC claims through one shared library, not three separate implementations. There is no
Docker daemon, live Keycloak/Qdrant/Ollama, or Hugging Face access in the environment this
was built in, so **nothing here has been run against a real cluster or a real
`docker compose up`** — every piece has instead been verified as rigorously as the sandbox
allows (real `TestClient`/`uvicorn`/MCP-client round trips, in-memory Postgres/Qdrant,
hand-crafted JWTs matching the seeded realm) and every gap that verification couldn't close is
called out explicitly rather than left implicit. See `docs/dev-setup.md`'s "What's stubbed
vs working" section for the honest, current list.

**What's working:**

- **Ingestion (FR-1..FR-9):** upload UI for PDF/DOCX/PPTX/XLSX/TXT/MD/HTML, mandatory
  Classification/Releasability/Access-scope tagging enforced server-side against the
  uploader's OIDC claims (not just hidden in the UI), structure-aware parsing and chunking,
  embedding via a self-hosted model, and durable async processing with real `queued →
  processing → embedded → pending_review` progress: `ingestion-api` validates the request,
  durably stores the original file, and returns immediately; a separate `ingestion-worker`
  service does the actual parse/chunk/embed/store work off a durable NATS JetStream queue
  (NFR-11), so a crash or restart mid-document doesn't strand it — and rejection/quarantine
  of corrupt, password-protected, oversized, or zip-bomb-shaped files.
- **Curation (FR-10..FR-16):** every submission is chunked and embedded immediately but
  stays excluded from retrieval until a curator approves it, scoped to the org(s) they hold
  curator authority for and capped by their own clearance *and* releasability. A curator
  can approve, reject with a reason, or correct the tags before approving — all three
  through the actual UI, not just the API. Every decision is audited and notifies the
  uploader in-app.
- **Metadata & tagging (FR-17..FR-23):** Classification and Releasability are each a
  single value per document chosen from admin-configurable controlled lists (add/retire/
  reorder without a code change), Access-scope is an independent org/group/user/`ALL_AUTHENTICATED`
  dimension that a document must pass *in addition to* classification/releasability, and
  identity-linked fields (uploader, owning org) auto-populate from claims rather than
  free text.
- **Retrieval & generation (FR-24..FR-29):** hybrid dense+BM25 retrieval fused with
  Reciprocal Rank Fusion, a cross-encoder reranking pass, and a mandatory, non-bypassable
  access filter — built server-side from the caller's verified claims, applied to both
  retrieval legs, never client-supplied — gating classification, releasability, access
  scope, *and* approval status before anything reaches Qdrant. Exposed to LibreChat as a
  real MCP server over streamable HTTP, reading the caller's identity from the forwarded
  Authorization header (OBO-exchanged or raw-forwarded), verified against the actual MCP
  client SDK end to end.
- **Monitoring & evaluation (FR-30..FR-32):** every ingestion, curation, and retrieval
  event is audit-logged keyed on the actor's OIDC identity, and a golden-query evaluation
  harness reports recall@K/precision@K plus a dedicated regression check that
  pending/rejected/superseded content never leaks into results regardless of the querying
  persona's clearance.
- **Dev and production packaging (NFR-9/NFR-10):** a one-command Docker Compose stack with
  a pre-seeded Keycloak realm and sample documents across every status/classification/
  access-scope combination, and a Helm chart scoped to just this project's new components
  (ingestion-api, ingestion-worker, orchestration-mcp, reranker-service, a dedicated
  embedding-service, Qdrant, NATS) that assumes the rest of MPNexus already exists.
- **Browser login for the ingestion UI:** a real OIDC Authorization Code + PKCE flow
  against Keycloak (`/auth/login` → `/auth/callback`) — tokens live server-side in a
  Postgres-backed session, refreshed transparently, never in browser-reachable storage.
  Replaces the earlier paste-a-token dev workaround. CSRF-protected (NFR-14): a
  double-submit cookie, checked on every state-changing route, that only applies to
  cookie-authenticated browser requests — bearer-token API/MCP callers are unaffected.
  See `ARCHITECTURE.md` Section 4.4.
- **Qdrant access control (NFR-15):** authenticated access required in every
  environment — a full read/write API key for `ingestion-api` and `ingestion-worker`
  (the two services that write to Qdrant), a read-only key for `orchestration-mcp`
  (least-privilege split, since it never writes to Qdrant).
- **Pinned image/model versions (NFR-16):** no more `:latest`/`main-latest`/bare-major-
  version tags in Compose or the Helm chart's default values — every external image is a
  specific recent release, researched at pin time (see `docs/dev-setup.md` for the exact
  list and version-by-version reasoning, including why LibreChat is deliberately held at
  the exact version its OBO integration recipe was verified against rather than bumped).
- **Separate DB credentials + append-only audit log (NFR-2/NFR-3):** the app and Keycloak
  no longer share a database or credentials in the dev stack, and the app's own DB role
  has `SELECT`/`INSERT` only on the audit log — `UPDATE`/`DELETE` require the bootstrap
  superuser, which day-to-day traffic never uses. Not yet run against a real environment —
  see `docs/dev-setup.md`, this is the riskiest of the hardening-batch changes.
- **Durable object storage for uploaded originals (NFR-12):** the raw file is written to a
  dedicated store (filesystem in dev, any S3-compatible endpoint in production) and its key
  recorded on the `Document` row before the upload request returns — durable independent of
  Qdrant's chunk vectors. `ingestion-worker` reads the original back from this store rather
  than taking it as a direct argument, so the file survives independently of whichever
  process/pod happens to handle it.
- **Durable, crash-resistant ingestion processing (NFR-11):** the parse/chunk/embed/store
  pipeline no longer runs in-process via FastAPI `BackgroundTasks` — `ingestion-api`
  publishes a job (just the document ID) to a NATS JetStream queue
  (`common/job_queue.py`), and a separate `ingestion-worker` service durably consumes it.
  A worker crash or restart mid-document doesn't silently strand it: the JetStream message
  is only acked on a terminal outcome (success, or a permanent parse/embed failure that
  lands the document in `failed`) — an unexpected/transient error is left un-acked, so
  JetStream redelivers it to another attempt.
- **Safe document supersession under partial failure (NFR-13):** re-ingestion/versioning
  (FR-7) already ordered its Qdrant/Postgres writes to avoid a window where neither the old
  nor the new version of a document is retrievable (confirmed by review, not just assumed).
  What was added: `approve()`/`reject()` now revert their Qdrant status write if the paired
  Postgres commit doesn't durably land, so a partial failure (a DB error, the old document's
  Qdrant delete failing, etc.) can't leave Qdrant showing a document as `approved` while
  Postgres and the curation queue both still call it `pending_review`.

**What's explicitly not done, and why:**

- **Keycloak's fine-grained token-exchange admin permission** for the OBO flow is a manual
  admin-console step (Keycloak 26.2+) — not expressible in a realm-export JSON or anything
  code can do for you.
- **`infra/librechat/librechat.yaml`'s exact schema** hasn't been validated against a
  running LibreChat 0.8.7 instance — only `orchestration-mcp`'s side of the MCP connection
  has been verified, using the real MCP client SDK standing in for LibreChat's client.
- **Helm/production wiring for browser login** — the new OIDC client-secret/redirect-URI/
  cookie-security env vars are dev Compose-only so far; see `docs/dev-setup.md`'s
  "Stubbed / TODO" list.
- **A concrete PyKMIP integration for encryption at rest (NFR-6)** — REQUIREMENTS.md names
  it only as a candidate, with no integration point, key rotation policy, or scope
  specified; building against that would mean guessing at a requirement rather than
  implementing one. What's actually addressable (pointing persistent volumes at an
  encrypted `StorageClass`) is done and documented.
- **NetworkPolicies, PodDisruptionBudgets, HorizontalPodAutoscalers** in the Helm chart —
  not called for by REQUIREMENTS.md; add them if your cluster's baseline requires them.
- **Nothing here has been run against a real Docker daemon, cluster, or `helm lint`** — see
  the "Status" paragraph above. This applies with extra force to `ingestion-worker`
  (NFR-11): it's the largest structural change in the P0 hardening batch — a new service,
  the ingestion request path moving from in-process `BackgroundTasks` to a NATS JetStream
  queue, and a changed Qdrant credential split — and was only verified with mocks (an
  in-memory SQLite DB, a mocked Qdrant/object-store/embedding client), never against a live
  NATS/Postgres/Qdrant stack. Run a real `docker compose up`, submit a document, and confirm
  it reaches `pending_review` via `ingestion-worker`'s logs and `GET /documents/{id}`
  polling before trusting it the way the rest of this batch can be.

## Architecture

**Ingestion:** a user uploads through `ingestion-api`'s web UI → mandatory tagging is
validated against their OIDC claims → the original file is durably stored (NFR-12) and the
document row lands in Postgres as `queued` → `ingestion-api` returns immediately and
publishes a job to NATS JetStream (NFR-11) rather than doing any further work itself → a
separate `ingestion-worker` service durably consumes that queue, parses, chunks, and embeds
the document (via Ollama), and writes chunk vectors and metadata into Qdrant tagged
`pending_review` → a curator with authority over that org/classification/releasability
reviews it in `ingestion-api`'s UI and approves, rejects, or corrects it → approval flips
the Qdrant chunks' status to `approved`, which is what actually makes them retrievable.

**Retrieval:** LibreChat calls `orchestration-mcp`'s `rag_search` MCP tool over streamable
HTTP, forwarding the user's identity in the connection's Authorization header (an
OBO-exchanged token, or a raw forwarded one) → the tool parses those claims and builds a
mandatory access filter server-side → a dense (Ollama embedding) and BM25 sparse leg are
queried against Qdrant in parallel with that same filter applied to both, fused via
Reciprocal Rank Fusion → the fused candidates are reranked by `reranker-service` → results,
with source/classification/releasability metadata attached, go back to LibreChat for
generation.

Keycloak (OIDC) issues the claims (`clearance`, `releasability`, `groups`, `org`,
`rag_roles`) that drive every one of those decisions, consumed identically by
`ingestion-api` (tagging, curation) and `orchestration-mcp` (retrieval) through one shared
claims-parsing/access-filter library (`services/common`), not two separate
implementations.

| Component | Role | FR/NFR coverage |
|---|---|---|
| `services/common` | Shared claims parsing, Section 6.3 metadata schema, Qdrant access-filter builder, DB models, object-store abstraction, NATS job-queue helpers | FR-18, FR-26, Section 6.1, NFR-11, NFR-12 |
| `services/ingestion-api` | Upload UI + API, mandatory tagging, curation queue + UI, admin-configurable lists — validates and durably stages a submission, then hands the actual pipeline off to `ingestion-worker` | FR-1..FR-23, C9, NFR-11..NFR-13 |
| `services/ingestion-worker` | Durable NATS JetStream consumer: parsing/chunking/embedding, Qdrant writes | FR-3..FR-6, NFR-11 |
| `services/orchestration-mcp` | FastMCP server exposing `rag_search` to LibreChat; hybrid retrieval, reranking, access enforcement, audit logging | FR-24..FR-31 |
| `services/reranker-service` | Cross-encoder reranking over the fused hybrid candidate pool | FR-25 |
| `infra/keycloak` | Seeded realm: claims schema, per-org curator client roles, test users | Section 6.2 |
| `infra/librechat`, `infra/litellm` | Throwaway dev configs so the MCP/OBO connection and generation path can be exercised locally | Section 7.7, NFR-9 |
| `scripts/` | Sample-data seeding and golden-query retrieval evaluation | NFR-9, FR-30/FR-32 |
| `helm/nexus-rag` | Production Kubernetes packaging, scoped to this project's new components | NFR-10 |

## Getting started

- **Local dev:** `docker compose up` — see **[docs/dev-setup.md](docs/dev-setup.md)** for
  prerequisites, seeded Keycloak users, a walkthrough of the full flow, and the exact
  what's-stubbed-vs-working list.
- **Production (Kubernetes):** `helm/nexus-rag/` — see
  **[helm/nexus-rag/README.md](helm/nexus-rag/README.md)** for prerequisites, install
  instructions, and what the chart deliberately does not do.

## Repo layout

```
nexus-rag/
  REQUIREMENTS.md            # source of truth for scope; everything above traces back to it
  docker-compose.yml         # one-command dev stack (NFR-9), incl. NATS (NFR-11)
  .env.example
  services/
    common/                  # shared claims/metadata/Qdrant-filter/object-store/job-queue library
    ingestion-api/           # upload + curation UI/API (FastAPI)
    ingestion-worker/        # durable parse/chunk/embed/store pipeline, NATS JetStream consumer (NFR-11)
    orchestration-mcp/       # retrieval MCP server (FastMCP)
    reranker-service/        # cross-encoder reranking API
  infra/
    keycloak/realm-export/   # seeded dev realm
    librechat/, litellm/     # throwaway dev configs for the MCP/OBO + generation path
  scripts/                   # sample-data seeding, retrieval evaluation harness
  helm/nexus-rag/            # production Helm chart (NFR-10)
  docs/dev-setup.md          # dev environment guide
```

## Security model, in one paragraph

Every Classification/Releasability/Access-scope decision — what a user may *assign* at
upload, what a curator may *approve*, and what a user may *see* at query time — is derived
from the same OIDC claims (`clearance`, `releasability`, `groups`, `org`, `rag_roles`),
evaluated server-side through one shared library, never trusted from anything the client
supplies. Qdrant's own RBAC is treated as coarse-grained only (Section 6.1); the actual
enforcement is a mandatory payload filter built from verified claims and injected into
every query before it reaches Qdrant, applied identically to both the dense and sparse
hybrid-retrieval legs so neither can be used to bypass it.
