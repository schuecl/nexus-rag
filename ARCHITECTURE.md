# Architecture

A visual companion to [REQUIREMENTS.md](REQUIREMENTS.md) — this document shows how the
pieces fit together and how data moves through them. It describes what's actually built
(see `docs/dev-setup.md`'s "What's stubbed vs working" for the authoritative, current
list) plus one flow that's designed but not yet implemented, called out explicitly where
it appears.

## 1. System overview

```mermaid
flowchart LR
    subgraph existing["Existing MPNexus (not built by this project)"]
        LibreChat["LibreChat<br/>(chat UI)"]
        LiteLLM["LiteLLM<br/>(gateway)"]
        VLLM["vLLM/Ollama<br/>(generation)"]
        Keycloak["Keycloak<br/>(OIDC IdP)"]
    end

    subgraph new["Built by this project"]
        IngestUI["ingestion-api<br/>(upload + curation UI/API)"]
        MCP["orchestration-mcp<br/>(rag_search MCP tool)"]
        Reranker["reranker-service"]
        EmbedOllama["embedding Ollama<br/>(dedicated instance)"]
        Qdrant[("Qdrant<br/>(vectors)")]
        Postgres[("Postgres<br/>(documents, audit, lists)")]
    end

    User(("Uploader / Curator")) -->|OIDC login| IngestUI
    Analyst(("Analyst")) --> LibreChat
    LibreChat -->|OBO-exchanged JWT| MCP
    LibreChat --> LiteLLM --> VLLM
    IngestUI -->|validate token| Keycloak
    MCP -->|validate token| Keycloak
    IngestUI --> EmbedOllama
    IngestUI --> Qdrant
    IngestUI --> Postgres
    MCP --> Qdrant
    MCP --> Reranker
    MCP --> EmbedOllama
    MCP --> Postgres
```

Everything in the `new` box is what this repo adds; everything in `existing` is assumed
already deployed and managed separately (NFR-10). The dev Compose stack (NFR-9) stands up
throwaway copies of the `existing` box too, so the whole diagram runs on a laptop.

## 2. Component inventory

| Component | Built here? | Tech | Role |
|---|---|---|---|
| `ingestion-api` | Yes | FastAPI + Jinja2/HTMX | Upload, mandatory tagging, curation queue, admin lists, notifications — both the browser UI and its REST API |
| `orchestration-mcp` | Yes | FastMCP (Python MCP SDK) | Exposes `rag_search` to LibreChat; builds the claims-based Qdrant filter, runs hybrid retrieval + rerank |
| `reranker-service` | Yes | FastAPI + sentence-transformers `CrossEncoder` | Scores/reorders fused retrieval candidates |
| `common` | Yes | Python package | Shared claims parsing, metadata schema, Qdrant filter builder, DB models — the single source of truth both services import rather than reimplement |
| embedding Ollama | Config only | Ollama | Dedicated embedding-serving instance (NFR-8: separate GPU allocation from generation) |
| Qdrant | Config only | Qdrant | Vector store — dense + BM25 named vectors per chunk, access-control payload fields |
| Postgres | Config only | Postgres | System of record: document status, audit log, notifications, admin-configurable classification/releasability lists |
| Keycloak | External | Keycloak | OIDC IdP — realm/users/roles seeded for dev, external in prod |
| LibreChat / LiteLLM / generation vLLM/Ollama | External | — | Existing MPNexus chat + generation stack this project layers onto |

## 3. Data model

```mermaid
erDiagram
    documents ||--o{ audit_log : "target_id"
    documents ||--o{ notifications : "document_id"
    documents |o--o| documents : "supersedes_document_id"

    documents {
        uuid id PK
        string uploader_sub
        string owner_org
        string classification
        string releasability
        json access_scope
        string status "queued|processing|embedded|pending_review|approved|rejected|superseded|failed"
        uuid supersedes_document_id FK
    }
    audit_log {
        uuid id PK
        string actor_sub
        string action
        string target_id
        json detail
    }
    notifications {
        uuid id PK
        string recipient_sub
        uuid document_id FK
        string decision
        bool read
    }
    classification_levels {
        int id PK
        string value
        int rank
        bool active
    }
    releasability_values {
        int id PK
        string value
        bool active
    }
```

Postgres is the transactional system of record (status, audit, admin lists). Qdrant holds
the actual chunk vectors — one point per chunk, two named vectors (`dense`, `bm25`) — plus
a copy of the access-control fields (`status`, `classification`, `releasability`,
`access_scope`) as payload, so retrieval can filter without a round trip to Postgres.

## 4. Major flows

### 4.1 Ingestion (FR-1..FR-9)

```mermaid
sequenceDiagram
    actor U as Uploader (browser)
    participant I as ingestion-api
    participant PG as Postgres
    participant O as embedding Ollama
    participant Q as Qdrant

    U->>I: POST /documents (file + Section 6.3 tags)
    I->>I: parse_claims(token), validate tags against claims (FR-18)
    I->>PG: insert Document(status=queued)
    I-->>U: 202 Accepted {status: queued}
    Note over I: BackgroundTasks — request already returned
    I->>I: parse -> chunk (app/parsing.py, app/chunking.py)
    I->>O: embed chunks
    I->>Q: upsert_chunks (status=pending_review in payload)
    I->>PG: update Document(status=embedded -> pending_review)
    U->>I: GET /documents/{id} (polls)
    I-->>U: current status
```

### 4.2 Curation (FR-10..FR-16)

```mermaid
sequenceDiagram
    actor C as Curator (browser)
    participant I as ingestion-api
    participant PG as Postgres
    participant Q as Qdrant

    C->>I: GET /curate (queue, scoped to curatable orgs)
    I->>PG: select Document where status=pending_review, org in curatable_orgs
    I-->>C: queue rows, inline correction fields
    C->>I: POST /curate/{id}/approve (optionally corrected tags)
    I->>I: re-check claims against (possibly corrected) tags — cap by clearance & releasability
    I->>Q: update chunk payload (status=approved, corrected tags if any)
    I->>PG: update Document(status=approved), insert audit_log, insert notification
    Note over I: reject follows the same path with a required reason,<br/>status=rejected, no Qdrant payload flip to approved
```

### 4.3 Query / retrieval (FR-24..FR-29)

```mermaid
sequenceDiagram
    actor A as Analyst
    participant LC as LibreChat
    participant KC as Keycloak
    participant M as orchestration-mcp
    participant Q as Qdrant
    participant R as reranker-service
    participant PG as Postgres

    A->>LC: chat message
    LC->>KC: OBO token exchange (RFC 8693, Section 7.7)
    KC-->>LC: token scoped to rag-app audience
    LC->>M: rag_search(query) over MCP, Authorization: Bearer <token>
    M->>M: parse_claims(token) -> build access filter (common/qdrant_filters.py)
    par dense leg
        M->>Q: Prefetch dense vector, filter applied
    and BM25 leg
        M->>Q: Prefetch bm25 vector, filter applied
    end
    Q-->>M: fused candidates (RRF)
    M->>R: rerank(query, candidates)
    R-->>M: reordered results
    M->>PG: audit_log insert (query, applied filter, result count)
    M-->>LC: results (or "no results pass the access filter" — FR-28)
    LC-->>A: grounded answer
```

The access filter (`status=approved` + `classification` at-or-below clearance +
`releasability` match + `access_scope` match) is built entirely server-side from the
verified token — never from anything the client/LibreChat supplies — which is what makes
FR-26 non-bypassable.

`orchestration-mcp` also exposes this same logic as a plain REST endpoint,
`POST /debug/rag_search`, for curl-based testing without an MCP client (§4.4's ingestion
UI has a `/search` page that's a thin proxy over this same endpoint, forwarding the
logged-in user's own session token — no enforcement logic duplicated in `ingestion-api`,
it's still all in `orchestration-mcp`).

### 4.4 Ingestion UI login

Replaces the old pasted-access-token dev workaround. Page routes (`GET /`, `/curate`, ...)
still render unauthenticated — there's no forced redirect on page load — but every
underlying fetch call (upload, curate, notifications) now rides a session cookie instead
of a manually-attached header. The nav shows "Log in" when logged out, or the current
user's `preferred_username` plus "Log out" when logged in.

```mermaid
sequenceDiagram
    actor U as User (browser)
    participant I as ingestion-api
    participant KC as Keycloak

    U->>I: GET /auth/login (clicked "Log in")
    I->>I: generate state + PKCE verifier, insert oauth_states row
    I-->>U: 302 to Keycloak authorize endpoint, state cookie set
    U->>KC: authenticate
    KC-->>U: 302 /auth/callback?code&state
    U->>I: GET /auth/callback
    I->>I: state == state cookie? oauth_states row exists?
    I->>KC: exchange code for tokens (client secret + PKCE verifier)
    KC-->>I: access_token, refresh_token, id_token
    I->>I: insert user_sessions row, set HttpOnly session-id cookie
    I-->>U: 302 / (now authenticated)
    Note over I: subsequent requests: cookie -> user_sessions row -> access_token<br/>(refreshed via refresh_token if expired) -> same parse_claims() as the header-auth path
    U->>I: GET /auth/logout
    I->>I: delete user_sessions row
    I-->>U: 302 to Keycloak end_session_endpoint (id_token_hint + post_logout_redirect_uri)
    Note over U,KC: ends the browser's Keycloak SSO session too, not just this app's --<br/>logging back in re-prompts for credentials
```

Implementation notes:
- `rag-app` is already a confidential client with a secret in the realm export, so no new
  Keycloak config was needed — `app/routes/auth.py` and `app/deps.py`.
- Tokens live in a new Postgres `user_sessions` table (`common/models.py`), not in the
  cookie itself — keeps the token out of JS-reachable storage and makes a session
  individually revocable. `oauth_states` is a matching short-lived table for the
  login-in-progress `state`/PKCE `code_verifier` pair.
- The existing header-based `get_current_user` path is untouched for API/MCP callers;
  it now checks the session cookie first and falls back to the Authorization header — no
  forked enforcement logic between browser and API callers. `get_current_user_optional`
  (used only by the three page routes, for the nav's username display) is the same
  resolution but returns `None` instead of raising on an anonymous visitor.
- The paste-a-token box was retired outright (not kept behind a flag) rather than running
  two parallel auth UX paths.
- Logout uses `id_token_hint` (the `id_token` captured at `/callback`) rather than just
  `client_id`, since newer Keycloak versions reject the latter for RP-initiated logout.
- Helm chart wiring: `externalKeycloak.clientId`/`.clientSecret` (Secret-backed, same
  pattern as `externalPostgres`) and `ingestionApi.oidcRedirectUri` (derived from
  `ingress.host`/`ingress.tls` if not set explicitly, via `_helpers.tpl`'s
  `nexus-rag.oidcRedirectUri` — fails the render rather than deploying a broken callback
  URL if neither is available) / `.cookieSecure`. Like the rest of the chart, unverified by
  `helm lint`/`helm template` — see `docs/dev-setup.md`'s "Stubbed / TODO" list.

### 4.5 Re-ingestion / versioning (FR-7)

```mermaid
sequenceDiagram
    actor U as Uploader
    actor C as Curator
    participant I as ingestion-api
    participant PG as Postgres
    participant Q as Qdrant

    U->>I: POST /documents (supersedes_document_id = old doc)
    I->>PG: validate_supersede_target — old doc approved, org/clearance/releasability match
    I->>PG: insert new Document(status=queued)
    Note over I: normal ingestion pipeline runs (4.1)
    C->>I: POST /curate/{new_id}/approve
    I->>I: re-check curator authority against the OLD document too
    I->>Q: delete old document's chunks
    I->>PG: old Document.status = superseded
    I->>PG: new Document.status = approved
    I->>PG: audit_log: document.supersede (old id, new id, curator)
```

## 5. Security model

Single enforcement principle: every claim-gated decision — what a user may *tag* a
document with (FR-18), what a curator may *approve* (FR-14), and what a query may
*retrieve* (FR-26) — is derived from the same verified OIDC claims via `common/claims.py`,
never from client-supplied values. Two independent enforcement points share one library
rather than reimplementing the check:

| Enforcement point | Where | What it checks |
|---|---|---|
| Ingest-time tagging | `ingestion-api` upload route | Classification/Releasability offered ≤ uploader's clearance/releasability |
| Curation | `ingestion-api` curate route | Approving curator holds `rag-curate:<org>` for the doc's org, and clearance/releasability cover the (possibly corrected) tags |
| Query-time retrieval | `orchestration-mcp` | Qdrant filter restricts to `approved` + classification ≤ clearance + releasability match + access_scope match |
| Audit | Both services, `audit_log` table | Every submit/approve/reject/supersede/query is recorded against the actor's `sub`, not a self-reported name |

## 6. Deployment topology

```mermaid
flowchart TB
    subgraph dev["Dev: docker compose (NFR-9)"]
        direction LR
        d1["postgres"] & d2["keycloak"] & d3["qdrant"] & d4["ollama"] & d5["ingestion-api"] & d6["orchestration-mcp"] & d7["reranker-service"] & d8["librechat + litellm<br/>(throwaway)"]
    end
    subgraph prod["Prod: Helm chart (NFR-10)"]
        direction LR
        p1["ingestion-api"] & p2["orchestration-mcp"] & p3["reranker-service"] & p4["embedding-service"] & p5["qdrant (StatefulSet)"]
        p6[["external Postgres<br/>(Secret ref)"]]
        p7[["external Keycloak"]]
        p8[["existing LibreChat/LiteLLM/vLLM"]]
    end
```

Dev stands up *everything*, including throwaway LibreChat/LiteLLM/Keycloak instances, so
the full OBO/MCP flow can be exercised locally. The Helm chart deploys only the boxes in
the `new` component table (§2) — Postgres and Keycloak are referenced via `values.yaml`
(`externalPostgres.existingSecret`, `externalKeycloak.issuerUrl`), not deployed by the
chart.

## 7. Known gaps

See `docs/dev-setup.md`'s "What's stubbed vs working" for the current, authoritative list
(kept there rather than duplicated here, since it changes as work lands). Notable ones as
of this writing: §4.4's browser OIDC login, Keycloak OBO admin-console steps that can't be
expressed in the realm-export JSON, and `librechat.yaml`'s schema not yet validated against
a running LibreChat instance.
