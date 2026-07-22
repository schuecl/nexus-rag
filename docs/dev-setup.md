# Local Dev Environment (NFR-9)

One-command stand-up of the nexus-rag stack for exercising the ingest → curate → query
flow on a workstation, with zero dependency on the production cluster. Every service is
wired together, the auth/tagging plumbing works end to end, submitted documents are
parsed, chunked, embedded, and made retrievable once approved (FR-3..FR-6), retrieval
genuinely fuses dense+BM25 hybrid search with a reranking pass (FR-24/FR-25), documents
can be versioned (FR-7), and `orchestration-mcp`'s MCP tool reads the caller's identity
from the connection's Authorization header rather than a client-supplied argument, the
way LibreChat's OBO/addUserJwtToken forwarding actually delivers it. See "What's stubbed
vs working" below for what's still open.

**Schema note:** this version writes chunks with two named Qdrant vectors (`dense` +
`bm25`) instead of one unnamed vector. If you have a Qdrant volume from before hybrid
search was added, run `docker compose down -v` first -- `ensure_collection` only
configures a collection when it doesn't already exist, so a stale volume won't pick up
the new schema on its own.

## Prerequisites

- Docker with Compose v2 (`docker compose version`)
- ~10GB free disk (Ollama models + HF reranker/BM25 model caches)
- Internet access on first run only, to pull base images and download the embedding/
  generation/reranker/BM25 models (the last two from Hugging Face — `ingestion-api` and
  `orchestration-mcp` both pull `Qdrant/bm25` via `fastembed` on first use). None of this
  is air-gapped yet — NFR-1 applies to the production Helm deployment (NFR-10), not this
  dev stack.

## Start the stack

```bash
cp .env.example .env
docker compose up --build
```

First boot takes a while: Keycloak imports the realm, `ollama-model-init` pulls
`nomic-embed-text` and `llama3.2:1b`, `reranker-service` downloads
`cross-encoder/ms-marco-MiniLM-L6-v2`, `ingestion-api`/`orchestration-mcp` each download
the tiny (~10MB) `Qdrant/bm25` sparse model on first use — all from Hugging Face — and
finally `seed-sample-data` submits and curates 7 sample documents through the real API
once everything above is healthy.

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

By the time `docker compose up` finishes, `seed-sample-data` has already run steps 1-2
below for you against 7 real documents (see "What's stubbed vs working"). To query them
immediately, get a `bob-query` token (step 3's instructions) and search for e.g.
`password rotation` or `VPN access` — or skip ahead to step 3 directly. The steps below
walk through the same flow manually, useful for understanding what the seed script
automated or for testing with your own file.

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

   Expect a `202` with `status: queued` — submission is accepted immediately and the
   actual parse/chunk/embed/store pipeline (FR-3..FR-6) runs in the background (FR-8),
   not before responding. Poll `GET /documents/<id>` (same bearer token) until `status`
   reaches `pending_review` (or `failed`, with a message in `processing_error` — try an
   unsupported extension or a corrupt/password-protected PDF to see this path; it's a
   202-then-`failed` now, not a synchronous 422 like before FR-8). The ingestion UI does
   this polling for you automatically after a browser upload. Try `classification=SECRET`
   as `alice-ingest` (only cleared to CUI) and confirm the *submission itself* is
   rejected with a 403 (FR-18) — that check is still synchronous, only parsing/embedding
   moved to the background.

2. **Curate** as `carol-curator` at http://localhost:8001/curate (or `GET/POST
   /curate/...` directly) — the pending doc from step 1 should appear (org match), and
   approve/reject should work. Confirm `bob-query`'s clearance-only token (no curator
   role) gets a 403 from `/curate/queue`. Approving flips the chunks' `status` in Qdrant
   to `approved` too (not just the Postgres row) — that's what actually makes them
   visible to queries. Then check http://localhost:8001/notifications as `alice-ingest`
   (paste her token) and confirm a notification about the decision is there (FR-15).

3. **Query** as `bob-query` against the debug endpoint with a phrase that appears in the
   document you submitted:

   ```bash
   curl -s -X POST "http://localhost:8002/debug/rag_search?query=<a+phrase+from+your+doc>&top_k=5" \
     -H "Authorization: Bearer $TOKEN"
   ```

   Expect `results` to contain the matching chunk(s), each with the source document's
   `applied_filter`-passing payload (classification, releasability, access_scope,
   filename, heading/page_or_slide). `hybrid_retrieval` and `reranking` in the response
   describe what actually ran — a dense+BM25 RRF fusion over the candidate pool, then a
   cross-encoder rerank via `reranker-service` (falls back to the fused order with a note
   if `reranker-service` is unreachable, rather than failing the query). Query as a user
   outside the document's `access_scope` (e.g. someone not in `USAREUR-AF` and the doc
   isn't tagged `PUBLIC`) and confirm `results` comes back empty — that's FR-26
   enforcement holding on *both* the dense and sparse legs, not a bug.

4. **Supersede** the document from step 1: submit a second file as `alice-ingest` with
   `supersedes_document_id` set to the first document's `id` (same form field at
   http://localhost:8001, or `-F supersedes_document_id=<id>` on the curl call from step
   1). Approve it as `carol-curator`. Confirm the original document's status is now
   `superseded` (`GET /documents/mine` as `alice-ingest`), that querying no longer
   returns its chunks, and that only the new version's chunks do. Try superseding a
   document that's still `pending_review`, or one outside `alice-ingest`'s org, and
   confirm both are rejected with a 403/404 rather than silently accepted (FR-7).

5. **Run the evaluation harness** against the seeded sample documents:

   ```bash
   docker compose --profile eval run --rm eval-retrieval
   ```

   Expect `mean recall@K` near 1.0 and `forbidden leaks: 0` — the golden set's negative
   cases (the `pending_review` and `rejected` sample documents) should never appear in
   results no matter how privileged the querying persona is. A `LEAK` in the per-query
   output is a real FR-26 regression, not just a quality miss, and the run exits non-zero
   if one occurs.

## What's stubbed vs working

**Working:**
- Claims parsing, the Section 6.3 metadata schema, and the Qdrant access-filter builder
  (`services/common`) — shared by both services, not implemented twice.
- Mandatory tagging enforced server-side against the caller's claims (FR-18), not just
  hidden in the UI.
- Submission → `pending_review` → curator queue → approve/reject/correct, scoped by org
  and capped by clearance (FR-10..FR-16), with an audit log entry per action (FR-31) —
  ingestion, curation, *and* retrieval events are all logged now: every `rag_search`
  call writes an entry keyed on the caller's identity, whether it succeeded (with the
  applied claims-based filter and result count), was denied (missing `rag-query` role,
  logged as `query.denied`), or hit an unreachable Qdrant.
- **Uploader notifications on curator decisions (FR-15)** — approving or rejecting a
  document writes an in-app `Notification` row for the uploader
  (`common/models.py`/`app/routes/notifications.py`), with the rejection reason
  included for rejections. No SMTP/email infra in this dev stack, so this is a
  discrete, markable-as-read record (`GET /notifications`, `POST
  /notifications/{id}/read`, both scoped to the recipient) rather than an email/push
  notification — a real notification the uploader doesn't have to already know a
  document ID to find, not just data that happens to be visible if you go looking.
- **Document parsing, chunking, embedding, and Qdrant storage (FR-3..FR-6)** —
  `services/ingestion-api/app/{parsing,chunking,embedding}.py`. Handles PDF, DOCX,
  PPTX, XLSX, TXT/MD, HTML; chunks respect section/heading/page/slide boundaries
  (~512 words, ~15% overlap — word-based, not a model-specific tokenizer).
  A curator's approve/reject (and any tag corrections made while approving) propagate
  to the chunks' Qdrant payload, not just the Postgres row (`common/qdrant_store.py`)
  — that's what actually changes query-time visibility.
- **Async ingestion pipeline with real progress states (FR-8)** — `POST /documents`
  validates the request synchronously (auth, mandatory tagging, FR-7 supersede-target
  checks) and returns `202 Accepted` with `status: queued` immediately; the actual
  parse/chunk/embed/store pipeline runs in the background
  (`app/routes/upload.py:_process_document`), moving the row through
  `queued → processing → embedded → pending_review`, or to `failed` with a message in
  `processing_error` if parsing or embedding errors out (NFR-7: caught, not left to
  crash the worker) — corrupt, password-protected, empty, or unsupported files land
  here instead of a synchronous 4xx like before this change. `GET /documents/{id}`
  (scoped to the uploader) polls current status; the ingestion UI polls it
  automatically after upload. Uses FastAPI's `BackgroundTasks`, not a durable queue —
  simple and adequate for this dev stack, but not crash-resilient: a process restart
  mid-processing leaves a document stuck in `processing` forever. A production
  deployment would want a real queue (Celery/RQ + a broker, or an outbox pattern) for
  that guarantee; noted here rather than built, to avoid adding a new stateful service
  to the dev stack for a dev-scale problem it doesn't actually have.
- **Hybrid dense+BM25 retrieval and reranking (FR-24/FR-25)** —
  `services/orchestration-mcp/app/rag_search.py` queries a dense semantic leg and a BM25
  sparse leg (`common/sparse_embedding.py`, Qdrant's own `fastembed`/`Qdrant/bm25` model)
  in parallel via Qdrant's native `Prefetch`/`FusionQuery` (Reciprocal Rank Fusion), with
  the access filter applied to *both* legs so neither can be used to bypass FR-26. The
  fused candidates are then reranked by `reranker-service` (`app/reranking.py`), with a
  graceful fallback to the fused order (noted in the response, not hidden) if that
  service is unreachable.
- **Re-ingestion/versioning (FR-7)** — an uploader can mark a submission as superseding
  an existing approved document (`supersedes_document_id`, validated server-side against
  the submitter's org/clearance/releasability, not just that the target exists —
  `common/versioning.py`). The actual swap happens atomically with the *new* version's
  curator approval, not at submission time: the old document's Qdrant chunks are deleted
  (no orphans/duplicates), its Postgres status flips to `superseded`, and a
  `document.supersede` audit entry records old/new document IDs and the approving
  curator. The old document stays fully live until that moment. The approving curator's
  authority is independently re-checked against the *old* document too (org + clearance),
  since a version can legitimately change classification.
- **MCP Authorization-header forwarding** — `orchestration-mcp`'s `rag_search` tool
  (`services/orchestration-mcp/app/server.py`) reads the bearer token from the
  streamable-http request's `Authorization` header via `ctx.request_context.request`,
  not a tool argument, so whatever LibreChat puts there (an OBO-exchanged token per
  `infra/librechat/librechat.yaml`'s `obo.scopes`, or a raw `addUserJwtToken`-forwarded
  one) reaches it correctly. Verified against the real `mcp` client SDK end to end
  (session init → tool call → claims parsed → access filter applied), not just read from
  source — that testing caught a real bug in how the MCP app was mounted (see the FR-7
  commit's sibling for the write-up) where the streamable-http session manager's task
  group was never started, so every MCP call would have 500'd. Fixed by adding `/health`
  and `/debug/rag_search` via FastMCP's own `custom_route` instead of wrapping the app in
  an outer Starlette `Mount`, which doesn't propagate lifespan to the mounted sub-app.
- Admin-configurable Classification/Releasability lists (C9) via `/admin/*`.
- Keycloak realm, seeded users/roles/claims, and the client role → `rag_roles` claim
  aggregation (Section 6.2).
- **Pre-seeded sample documents (NFR-9)** — the `seed-sample-data` one-shot service
  (`scripts/seed_sample_data.py`) runs automatically after `ingestion-api`, Keycloak, and
  the embedding model are all ready, submitting 7 documents through the real ingestion
  API as the seeded users and driving them to every `Status` value: `approved` (a
  `PUBLIC` notice, an org-scoped policy, a `Signal-Corps`-scoped `SECRET` document
  submitted by `dave-admin`), `pending_review` (left unreviewed on purpose),
  `rejected` (with a reason), and `superseded` (a two-version FR-7 demo). See "Exercising
  the flow" below for how to query them immediately after `docker compose up`.
- **Retrieval evaluation harness (FR-30/FR-32)** — `scripts/evaluate_retrieval.py` runs a
  fixed set of golden queries (`scripts/golden_queries.json`, keyed to the seeded sample
  documents) through the real retrieval pipeline and reports recall@K, precision@K, and
  first-relevant-rank, plus a separate check that pending/rejected/superseded content
  never leaks into results regardless of the querying persona's clearance (a regression
  check on FR-26, not just a quality metric). Not started automatically — run on demand
  with `docker compose --profile eval run --rm eval-retrieval` (FR-32's "periodically
  re-evaluate"). This is a lighter, judge-free stand-in for the `ragas` library itself
  (Section 7.6): RAGAS's more interesting metrics (faithfulness, LLM-judged context
  precision) need a configured LLM judge and wired-up generation, neither of which exists
  in this repo yet.

**Stubbed / TODO (see inline `TODO` comments at each site):**
- Keycloak's fine-grained token-exchange admin permission (required on top of the
  client's `standard.token.exchange.enabled` attribute — see the `_comment` in the realm
  export) still needs a manual admin-console step, and `infra/librechat/librechat.yaml`'s
  exact schema hasn't been validated against a running LibreChat 0.8.7 instance (only
  `orchestration-mcp`'s side of the OBO connection has been verified, using the real MCP
  client SDK standing in for LibreChat's MCP client).
- Full OIDC Authorization Code login flow for the ingestion UI's browser pages (uses a
  pasted-token workaround instead, see above).

## Resetting

```bash
docker compose down -v   # also wipes Postgres/Qdrant/Ollama/reranker-cache volumes
```
