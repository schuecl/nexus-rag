# Local Dev Environment (NFR-9)

One-command stand-up of the nexus-rag stack for exercising the ingest → curate → query
flow on a workstation, with zero dependency on the production cluster. Every service is
wired together, the auth/tagging plumbing works end to end, and submitted documents are
now actually parsed, chunked, embedded, and made retrievable once approved (FR-3..FR-6).
Hybrid dense+BM25 fusion/reranking and the OBO header-forwarding wiring are still `TODO`
stubs. See "What's stubbed vs working" below.

## Prerequisites

- Docker with Compose v2 (`docker compose version`)
- ~10GB free disk (Ollama models + HF reranker model cache)
- Internet access on first run only, to pull base images and download the embedding/
  generation/reranker models. None of this is air-gapped yet — NFR-1 applies to the
  production Helm deployment (NFR-10), not this dev stack.

## Start the stack

```bash
cp .env.example .env
docker compose up --build
```

First boot takes a while: Keycloak imports the realm, `ollama-model-init` pulls
`nomic-embed-text` and `llama3.2:1b`, and `reranker-service` downloads
`cross-encoder/ms-marco-MiniLM-L6-v2` from Hugging Face.

| Service | URL | Notes |
|---|---|---|
| Keycloak admin console | http://localhost:8080 | login `admin` / `admin` (`.env`) |
| Ingestion UI | http://localhost:8001 | upload form + curation queue (paste-a-token, see below) |
| orchestration-mcp debug API | http://localhost:8002 | `/health`, `/debug/rag_search` |
| reranker-service | http://localhost:8003 | `/health`, `/rerank` |
| Qdrant | http://localhost:6333/dashboard | |
| LibreChat | http://localhost:3080 | throwaway, log in via Keycloak |
| LiteLLM | http://localhost:4000 | throwaway gateway in front of Ollama |

## Seeded Keycloak users (realm `nexus-rag`)

All dev-only, password `devpass123` for every account — **never reuse these in a real
environment.**

| Username | Roles | Clearance | Org | Purpose |
|---|---|---|---|---|
| `alice-ingest` | `rag-ingest` | CUI | USAREUR-AF | ingest-only |
| `bob-query` | `rag-query` | SECRET | USAREUR-AF | query-only |
| `carol-curator` | `rag-query`, `rag-curate:USAREUR-AF` | SECRET | USAREUR-AF | curator scoped to one org |
| `dave-admin` | all roles + both curator orgs | TOP SECRET | USAREUR-AF | admin |

## Getting a token for API testing (dev-only password grant)

The ingestion UI's browser pages take a pasted bearer token instead of a full OIDC
login redirect (that flow isn't implemented in this skeleton — see gaps below). Get one
with:

```bash
curl -s http://localhost:8080/realms/nexus-rag/protocol/openid-connect/token \
  -d grant_type=password \
  -d client_id=rag-app \
  -d client_secret=dev-rag-app-secret \
  -d username=alice-ingest \
  -d password=devpass123 \
  | jq -r .access_token
```

Swap `username`/`password` for any seeded user above.

## Exercising the flow

1. **Submit a document** as `alice-ingest`, either through http://localhost:8001 (paste
   the token from above into the field at the top of the page) or directly:

   ```bash
   TOKEN=$(...)  # from above
   curl -s http://localhost:8001/documents \
     -H "Authorization: Bearer $TOKEN" \
     -F file=@/path/to/some.pdf \
     -F classification=CUI \
     -F 'releasability=["REL TO USA/FVEY"]' \
     -F 'access_scope=["USAREUR-AF"]' \
     -F source_originator="Test Org" \
     -F doc_type="SOP"
   ```

   Expect `status: pending_review` and a non-zero `chunk_count` — the file was parsed,
   chunked, embedded, and its (not-yet-visible) chunks written to Qdrant with
   `status: pending_review`. Try `classification=SECRET` as `alice-ingest` (only cleared
   to CUI) and confirm it's rejected with a 403 (FR-18). Try an unsupported extension or
   a corrupt/password-protected PDF and confirm a 422 rather than a 500 (FR-9).

2. **Curate** as `carol-curator` at http://localhost:8001/curate (or `GET/POST
   /curate/...` directly) — the pending doc from step 1 should appear (org match), and
   approve/reject should work. Confirm `bob-query`'s clearance-only token (no curator
   role) gets a 403 from `/curate/queue`. Approving flips the chunks' `status` in Qdrant
   to `approved` too (not just the Postgres row) — that's what actually makes them
   visible to queries.

3. **Query** as `bob-query` against the debug endpoint with a phrase that appears in the
   document you submitted:

   ```bash
   curl -s -X POST "http://localhost:8002/debug/rag_search?query=<a+phrase+from+your+doc>&top_k=5" \
     -H "Authorization: Bearer $TOKEN"
   ```

   Expect `results` to contain the matching chunk(s), each with the source document's
   `applied_filter`-passing payload (classification, releasability, access_scope,
   filename, heading/page_or_slide). Query as a user outside the document's
   `access_scope` (e.g. someone not in `USAREUR-AF` and the doc isn't tagged `PUBLIC`)
   and confirm `results` comes back empty — that's FR-26 enforcement, not a bug.
   `hybrid_retrieval`/`reranking` in the response stay as `TODO` notes; this is a
   dense-only match today.

## What's stubbed vs working

**Working:**
- Claims parsing, the Section 6.3 metadata schema, and the Qdrant access-filter builder
  (`services/common`) — shared by both services, not implemented twice.
- Mandatory tagging enforced server-side against the caller's claims (FR-18), not just
  hidden in the UI.
- Submission → `pending_review` → curator queue → approve/reject/correct, scoped by org
  and capped by clearance (FR-10..FR-16), with an audit log entry per action (FR-31,
  partial — only ingestion/curation events, not yet retrieval).
- **Document parsing, chunking, embedding, and Qdrant storage (FR-3..FR-6)** —
  `services/ingestion-api/app/{parsing,chunking,embedding}.py`. Handles PDF, DOCX,
  PPTX, XLSX, TXT/MD, HTML; chunks respect section/heading/page/slide boundaries
  (~512 words, ~15% overlap — word-based, not a model-specific tokenizer); corrupt,
  password-protected, empty, or unsupported files fail with a 4xx (FR-9), not a 500.
  A curator's approve/reject (and any tag corrections made while approving) propagate
  to the chunks' Qdrant payload, not just the Postgres row (`common/qdrant_store.py`)
  — that's what actually changes query-time visibility.
- **`rag_search` returns real results** once a document is approved — dense-only, but
  genuinely matching against embedded content and enforcing the claims-based filter,
  not just building it against an empty collection.
- Admin-configurable Classification/Releasability lists (C9) via `/admin/*`.
- `reranker-service` — fully functional `/rerank` endpoint (not yet called from
  `rag_search`, see below).
- Keycloak realm, seeded users/roles/claims, and the client role → `rag_roles` claim
  aggregation (Section 6.2).

**Stubbed / TODO (see inline `TODO` comments at each site):**
- Hybrid dense+BM25 fusion and reranking (FR-24/FR-25) —
  `services/orchestration-mcp/app/rag_search.py`. Still a dense-only Qdrant query;
  `reranker-service` exists but isn't called from the retrieval path yet.
- Re-ingestion/versioning (FR-7) — replacing an outdated document's vectors without
  orphaning old ones isn't implemented; re-submitting the "same" document today just
  creates an unrelated second `Document` row and chunk set.
- Ingestion is synchronous within the request — no `queued`/`processing` states (FR-8),
  just pending_review-on-success or a 4xx-on-failure. A real deployment would move
  parsing/chunking/embedding to a background worker.
- `orchestration-mcp`'s MCP tool takes the bearer token as an explicit argument rather
  than reading a forwarded/OBO-exchanged header — see the `TODO` in
  `services/orchestration-mcp/app/server.py`. LibreChat's OBO config in
  `infra/librechat/librechat.yaml` is written to the current understanding of the
  0.8.7 schema but hasn't been validated against a running instance. Keycloak's version
  is confirmed to support RFC 8693 token exchange (REQUIREMENTS.md Section 7.7), but
  the fine-grained token-exchange admin permission (required on top of the
  `standard.token.exchange.enabled` client attribute — see the `_comment` in the realm
  export) still needs a manual admin-console step.
- No pre-seeded sample documents yet (NFR-9 asks for a range of Classification/
  Releasability/Access-scope/Status combinations) — FR-3..FR-6 exist now, so this is
  unblocked, just not done.
- RAGAS evaluation harness (FR-30/FR-32) not started.
- Full OIDC Authorization Code login flow for the ingestion UI's browser pages (uses a
  pasted-token workaround instead, see above).

## Resetting

```bash
docker compose down -v   # also wipes Postgres/Qdrant/Ollama/reranker-cache volumes
```
