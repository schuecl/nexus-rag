# nexus-rag

[![CI](https://github.com/schuecl/nexus-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/schuecl/nexus-rag/actions/workflows/ci.yml)
[![E2E](https://github.com/schuecl/nexus-rag/actions/workflows/e2e.yml/badge.svg)](https://github.com/schuecl/nexus-rag/actions/workflows/e2e.yml)
[![CodeQL](https://github.com/schuecl/nexus-rag/actions/workflows/codeql.yml/badge.svg)](https://github.com/schuecl/nexus-rag/actions/workflows/codeql.yml)
[![Security](https://github.com/schuecl/nexus-rag/actions/workflows/security.yml/badge.svg)](https://github.com/schuecl/nexus-rag/actions/workflows/security.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](CLAUDE.md)

Air-gapped Retrieval-Augmented Generation (RAG) pipeline for **MPNexus** — a DoD
Kubernetes environment already running LibreChat, LiteLLM, Keycloak, and vLLM/Ollama.
nexus-rag adds document ingestion, mandatory classification/releasability tagging, curator
review, and claims-based access-controlled retrieval on top of that stack, exposed to
LibreChat as a custom MCP tool.

Every access decision — what a user may assign at upload, what a curator may approve, and
what a user may see at query time — is derived server-side from the caller's verified OIDC
claims through one shared library, never trusted from the client.

> **Documentation:** requirements and design constraints in
> **[REQUIREMENTS.md](REQUIREMENTS.md)**; component diagram, data model, and per-flow
> sequence diagrams in **[ARCHITECTURE.md](ARCHITECTURE.md)**. This README is a snapshot of
> what's built, not a plan — see [Project status](#project-status) for how confidently each
> part has been verified.

## What it does

- **Ingestion (FR-1..FR-9, FR-34):** upload UI for PDF/DOCX/PPTX/XLSX/TXT/MD/HTML with
  mandatory, server-side-enforced Classification/Releasability/Access-scope tagging;
  structure-aware parsing and chunking; embedding via a self-hosted model; and durable
  async processing with real `queued → processing → embedded → pending_review` progress.
  Corrupt, password-protected, oversized, and zip-bomb-shaped files are rejected.
  Selecting more than one file submits them as a batch sharing one set of tags, each
  still stored/embedded/reviewed independently (FR-34).
- **Curation (FR-10..FR-16):** every submission stays excluded from retrieval until a
  curator approves it — scoped to the org(s) they hold curator authority for and capped by
  their own clearance *and* releasability. Approve, reject-with-reason, or correct-the-tags,
  all through the UI; every decision is audited and notifies the uploader. Any curator with
  authority over an already-approved document can also single-handedly suspend it back to
  pending review — reversible, and separate from the two-person-gated destructive purge
  path (#478). Curators also see
  an advisory box (never a gate) surfacing marking-mismatch, precedent, hidden-instruction,
  and PII/sensitive-data findings — some regex-based, some opt-in LLM-assisted — next to the
  approve/reject controls (see [ARCHITECTURE.md §4.6](ARCHITECTURE.md#46-tagging-advisory-pipeline-issue-138-family)).
- **Metadata & tagging (FR-17..FR-23):** Classification and Releasability are single values
  from admin-configurable controlled lists; Access-scope is an independent
  org/group/user/`ALL_AUTHENTICATED` dimension checked *in addition to* them; identity
  fields (uploader, owning org) auto-populate from claims.
- **Retrieval & generation (FR-24..FR-29):** hybrid dense+BM25 retrieval fused with
  Reciprocal Rank Fusion, a cross-encoder reranking pass, and a mandatory, non-bypassable
  access filter — built server-side from verified claims, applied to *both* retrieval legs —
  gating classification, releasability, access scope, and approval status before anything
  reaches Qdrant. Exposed to LibreChat as an MCP server over streamable HTTP.
- **Monitoring & evaluation (FR-30..FR-32):** every ingestion, curation, and retrieval event
  is audit-logged by OIDC identity. The deterministic golden-query harness reports
  recall@K/precision@K and fails on pending/rejected/superseded leaks; a separate
  local-judge harness drives the real LibreChat generation path and reports contextual
  relevance/recall/precision, faithfulness, answer quality, and citation validity (#74).

**Operational & security hardening:**

- Durable, crash-resistant ingestion via a NATS JetStream queue and a separate
  `ingestion-worker` (NFR-11); uploaded originals in dedicated object storage (NFR-12);
  safe supersession under partial failure (NFR-13).
- Browser login via a real OIDC Authorization Code + PKCE flow with server-side sessions and
  CSRF protection (NFR-14) — no tokens in browser-reachable storage.
- Authenticated Qdrant access with a least-privilege read/write vs read-only key split
  (NFR-15); pinned image/model versions, no floating tags (NFR-16); separate DB credentials
  and an append-only audit log (NFR-2/NFR-3).
- One-command Docker Compose dev stack and a Helm chart scoped to this project's components
  (NFR-9/NFR-10).

## Quick start

```bash
docker compose up
```

See **[docs/dev-setup.md](docs/dev-setup.md)** for prerequisites, the seeded Keycloak users,
a walkthrough of the full ingest → curate → retrieve flow, and what to expect. For the
Kubernetes path, see **[helm/nexus-rag/README.md](helm/nexus-rag/README.md)**.

## Architecture

**Ingestion:** upload through `ingestion-api`'s UI → tagging validated against OIDC claims →
original durably stored (NFR-12), row lands in Postgres as `queued` → `ingestion-api`
publishes a job to NATS JetStream (NFR-11) and returns → `ingestion-worker` consumes it,
parses/chunks/embeds (via Ollama), writes chunk vectors to Qdrant tagged `pending_review` →
a curator reviews and approves/rejects/corrects → approval flips the chunks to `approved`,
which is what makes them retrievable.

**Retrieval:** LibreChat calls `orchestration-mcp`'s `rag_search` tool over streamable HTTP,
forwarding the user's identity in the Authorization header (OBO-exchanged or raw) → the tool
parses claims and builds a mandatory access filter server-side → dense (Ollama) and BM25
sparse legs query Qdrant in parallel with that filter on both, fused via RRF → the fused
candidates are reranked by `reranker-service` → results with source/classification metadata
go back to LibreChat for generation.

Keycloak (OIDC) issues the claims (`clearance`, `releasability`, `groups`, `org`,
`rag_roles`) that drive every decision, consumed identically by `ingestion-api` and
`orchestration-mcp` through one shared library (`services/common`).

| Component | Role | FR/NFR coverage |
|---|---|---|
| `services/common` | Shared claims parsing, metadata schema, Qdrant access-filter builder, DB models, object-store + NATS helpers | FR-18, FR-26, §6.1, NFR-11, NFR-12 |
| `services/ingestion-api` | Upload UI + API, mandatory tagging, curation queue + UI, admin-configurable lists | FR-1..FR-23, NFR-11..NFR-13 |
| `services/ingestion-worker` | Durable NATS JetStream consumer: parsing/chunking/embedding, Qdrant writes | FR-3..FR-6, NFR-11 |
| `services/orchestration-mcp` | MCP server (MCP SDK 2.x `MCPServer`, #288) exposing `rag_search`; hybrid retrieval, reranking, access enforcement, audit logging | FR-24..FR-31 |
| `services/reranker-service` | Cross-encoder reranking over the fused candidate pool | FR-25 |
| `infra/keycloak` | Seeded realm: claims schema, per-org curator roles, test users | §6.2 |
| `infra/librechat`, `infra/litellm` | Throwaway dev configs for the MCP/OBO + generation path | §7.7, NFR-9 |
| `scripts/` | Sample-data seeding, deterministic retrieval evaluation, and local-judge Q→C→A evaluation | NFR-9, FR-30/FR-32 |
| `helm/nexus-rag` | Production Kubernetes packaging, scoped to this project's components | NFR-10 |

## Security model

Every Classification/Releasability/Access-scope decision is derived from the same OIDC
claims (`clearance`, `releasability`, `groups`, `org`, `rag_roles`), evaluated server-side
through one shared library, never trusted from client input. Qdrant's own RBAC is treated as
coarse-grained only (§6.1); the real enforcement is a mandatory payload filter built from
verified claims and injected into every query before it reaches Qdrant, applied identically
to both the dense and sparse hybrid-retrieval legs so neither can bypass it.

## Repo layout

```
nexus-rag/
  REQUIREMENTS.md            # source of truth for scope
  ARCHITECTURE.md            # diagrams, data model, per-flow sequences
  docker-compose.yml         # one-command dev stack (NFR-9), incl. NATS (NFR-11)
  services/
    common/                  # shared claims/metadata/Qdrant-filter/object-store/job-queue library
    ingestion-api/           # upload + curation UI/API (FastAPI)
    ingestion-worker/        # durable parse/chunk/embed/store pipeline (NATS JetStream consumer)
    orchestration-mcp/       # retrieval MCP server (MCP SDK 2.x MCPServer)
    reranker-service/        # cross-encoder reranking API
  infra/
    keycloak/realm-export/   # seeded dev realm
    librechat/, litellm/     # throwaway dev configs
  scripts/                   # sample-data seeding, retrieval and Q→C→A evaluation harnesses
  helm/nexus-rag/            # production Helm chart (NFR-10)
  docs/                      # dev-setup.md, testing.md
    nist-ai-rmf/              # NIST AI RMF compliance documentation set
```

## Documentation

All of the below is also published as a searchable MkDocs site — build it locally with
`pip install --require-hashes -r docs/requirements.txt && mkdocs serve` (issue #561).

- **[REQUIREMENTS.md](REQUIREMENTS.md)** — scope, functional/non-functional requirements, open questions
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — component diagram, data model, per-flow sequences
- **[docs/architecture/diagrams.md](docs/architecture/diagrams.md)** — Mermaid diagrams of the full system: every application, both data pipelines, the tagging model, lifecycle, and Helm topology (#129)
- **[docs/dev-setup.md](docs/dev-setup.md)** — local stack, seeded users, full walkthrough, and the honest what's-stubbed-vs-working list
- **[docs/testing.md](docs/testing.md)** — the test pyramid, coverage/mutation policy, and known gaps
- **[docs/governance.md](docs/governance.md)** — implemented data controls, the DoD classified-information profile, and explicitly unresolved authorization/marking gaps
- **[docs/nist-ai-rmf/README.md](docs/nist-ai-rmf/README.md)** — NIST AI RMF compliance documentation set: governance policy, risk register, impact assessment, system inventory, and the audit evidence index
- **[SECURITY.md](SECURITY.md)** — how to report a vulnerability
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — workflow, CI gates, and how to run the suites

## Project status

The full ingest → tag → curate → retrieve flow works end to end against every functional
requirement (FR-1..FR-34), with claims-based access control enforced server-side at every
stage through one shared library.

Confidence is labeled honestly throughout the docs as *implemented*, *tested against mocks*,
or *validated against a live environment* (see
[docs/dev-setup.md](docs/dev-setup.md#whats-stubbed-vs-working)). Highlights:

- **Validated against a real `docker compose up`:** the whole pipeline, including NFR-11's
  durable queue and NFR-12's object storage. That run surfaced and fixed eight real bugs and
  confirmed LibreChat OIDC login and the OBO token-exchange path live — the full history is
  in [docs/dev-setup.md](docs/dev-setup.md#live-validation-history).
- **Not yet verified live:** the Helm chart against a real cluster / `helm lint`; the
  NFR-2/NFR-3 DB-hardening against a real environment; PyKMIP encryption-at-rest
  (NFR-6, unscoped in REQUIREMENTS.md).

The full, current list — with per-item confidence labels and rationale — lives in
[docs/dev-setup.md](docs/dev-setup.md#whats-stubbed-vs-working).

## Contributing

Contributions are welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for the workflow and
CI gates, and the **[Code of Conduct](CODE_OF_CONDUCT.md)**. Licensed under
**[Apache-2.0](LICENSE)**.
