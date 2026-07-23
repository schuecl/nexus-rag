# Local Dev Environment (NFR-9)

One-command stand-up of the nexus-rag stack for exercising the ingest → curate → query
flow on a workstation, with zero dependency on the production cluster. Every service is
wired together, the auth/tagging plumbing works end to end, submitted documents are
parsed, chunked, embedded, and made retrievable once approved (FR-3..FR-6), retrieval
genuinely fuses dense+BM25 hybrid search with a reranking pass (FR-24/FR-25), documents
can be versioned (FR-7), and `orchestration-mcp`'s MCP tool reads the caller's identity
from the connection's Authorization header rather than a client-supplied argument, the
way LibreChat's OBO/addUserJwtToken forwarding actually delivers it. **Confirmed against
a real `docker compose up`** (not just inspected as code) end to end: upload through the
ingestion UI with a real browser-obtained token, curation, and search all manually
verified working -- see the Keycloak realm bullet below for the eight real bugs that
stood between "should work" and actually working. See "What's stubbed vs working" below
for what's still open.

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
  dev stack. Same goes for NFR-6 (encryption at rest): Compose's Postgres/Qdrant volumes
  are plain local Docker volumes with no encryption, fine for throwaway dev data — see
  `helm/nexus-rag/README.md`'s "Encryption at rest" section for the production posture.

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
| Keycloak health/metrics | http://localhost:9000/health/ready | `KC_HEALTH_ENABLED=true` moves `/health*` onto Keycloak's separate management interface (default port 9000) rather than 8080 -- what the `keycloak` service's Compose healthcheck actually probes |
| Ingestion UI | http://localhost:8001 | upload form, curation queue, and a search page (click "Log in", real Keycloak login) |
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

The ingestion UI's browser pages now use a real Keycloak login redirect (click "Log in" —
ARCHITECTURE.md Section 4.4); this section is for curl/API testing, which still needs a
raw bearer token. Get one with:

```bash
curl -s http://localhost:8080/realms/nexus-rag/protocol/openid-connect/token \
  -d grant_type=password \
  -d client_id=rag-app \
  -d client_secret=dev-rag-app-secret \
  -d username=alice-ingest \
  -d password=devpass123 \
  | jq -r .access_token
```

A token requested this way (via `localhost:8080`, i.e. from outside the Compose network) carries a different `iss` claim than one requested via `keycloak:8080` (i.e. from another container, like `scripts/_keycloak.py`) -- Keycloak's default (no fixed `KC_HOSTNAME`) behavior stamps `iss` with whichever hostname the request actually used. `ingestion-api`/`orchestration-mcp` accept both (`OIDC_ISSUERS`, a comma-separated allowlist -- see `common/claims.py`), found and fixed after a real "invalid token: Invalid issuer" error pasting a `localhost`-obtained token into the ingestion UI, which validated against only the `keycloak:8080` form at the time.

Swap `username`/`password` for any seeded user above.

## Exercising the flow

By the time `docker compose up` finishes, `seed-sample-data` has already run steps 1-2
below for you against 7 real documents (see "What's stubbed vs working"). To query them
immediately, get a `bob-query` token (step 3's instructions) and search for e.g.
`password rotation` or `VPN access` — or skip ahead to step 3 directly. The steps below
walk through the same flow manually, useful for understanding what the seed script
automated or for testing with your own file.

1. **Submit a document** as `alice-ingest`, either through http://localhost:8001 (click
   "Log in", authenticate as `alice-ingest` at Keycloak) or directly:

   ```bash
   TOKEN=$(...)  # from above
   curl -s http://localhost:8001/documents \
     -H "Authorization: Bearer $TOKEN" \
     -F file=@/path/to/some.pdf \
     -F classification=CUI \
     -F 'releasability=REL TO USA/FVEY' \
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
   (log in as her — or log out and back in as a different seeded user to switch) and
   confirm a notification about the decision is there (FR-15).

3. **Query** as `bob-query` with a phrase that appears in the document you submitted,
   either through http://localhost:8001/search (log in as `bob-query`) or directly
   against the debug endpoint:

   ```bash
   curl -s -X POST "http://localhost:8002/debug/rag_search?query=<a+phrase+from+your+doc>&top_k=5" \
     -H "Authorization: Bearer $TOKEN"
   ```

   The UI page is a thin proxy over this same endpoint (`app/routes/search.py`), forwarding
   your logged-in session's own token — same access filter either way.

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
  and capped by clearance *and* releasability (FR-10..FR-16, FR-14.1 mirroring FR-18's
  uploader-side check) — a curator missing a document's releasability caveat is denied
  the same as one lacking the classification level, on both approve and reject, and the
  check re-runs against corrected tags if the curator adjusts them before approving.
  The **correct** action (FR-13) is in `/curate`'s UI itself now, not just the API: each
  queued document gets inline Classification/Releasability dropdowns (live from the same
  admin-configurable lists as the upload form, C9/FR-17) and an editable access-scope
  field, pre-filled with the uploader's original tags; approving only sends a correction
  if something was actually changed. With an audit log entry per action (FR-31) —
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
  (~512 words, ~15% overlap — word-based, not a model-specific tokenizer; both are
  env-configurable per FR-4 via `CHUNK_TARGET_WORDS`/`CHUNK_OVERLAP_RATIO` --
  `.env.example`/`docker-compose.yml` here, `ingestionApi.chunkTargetWords`/
  `chunkOverlapRatio` in the Helm chart).
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
  crash the worker) — corrupt, password-protected, empty, unsupported, or zip-bomb-shaped
  (`app/parsing.py`'s `_check_zip_bomb`: a `.docx`/`.pptx`/`.xlsx` whose ZIP entries would
  decompress past 200MB or at a >200:1 ratio is rejected before python-docx/python-pptx/
  openpyxl ever touch it, since `MAX_UPLOAD_BYTES` only bounds the *compressed* upload)
  files land here instead of a synchronous 4xx like before this change. `MAX_UPLOAD_BYTES`
  itself is env-configurable (FR-9's "configurable size limit"), default 50MB -- see
  `.env.example`/`docker-compose.yml` here, `ingestionApi.maxUploadBytes` in the Helm
  chart. `GET /documents/{id}`
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
  authority is independently re-checked against the *old* document too (org, clearance,
  and releasability), since a version can legitimately change classification.
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
- **Admin-configurable Classification/Releasability lists (C9)** via `/admin/*`
  (`rag-admin` only) — add, retire (soft-delete via an `active` flag, not a hard
  delete, so existing documents/audit history keep referencing the value), or
  reorder without a code change or redeploy. The upload UI's dropdowns
  (`GET /`) live-query these same tables (active values only, classification
  ordered by rank), not a hardcoded list, so an admin change is reflected on
  the next page load.
- **Keycloak realm, seeded users/roles/claims, and the client role → `rag_roles` claim
  aggregation (Section 6.2)** -- exercised against a real `docker compose up` (not just
  inspected as JSON), which surfaced eight real, independently-fixed failures. All are
  fixed, and the full flow -- realm import, a healthy `keycloak` container, password-grant
  login, and a token actually accepted by `ingestion-api`/`orchestration-mcp` -- is
  confirmed end to end against a real running stack, not assumed:
  1. **`_comment`-style fields break realm import outright.** Keycloak's importer uses
     strict JSON deserialization and rejects any unrecognized property.
  2. **Healthcheck probing the wrong port.** `KC_HEALTH_ENABLED=true` serves `/health*`
     on a separate management port (9000), not 8080 -- Keycloak itself was serving real
     traffic fine the whole time; only the healthcheck was pointed wrong, permanently
     blocking every service with `depends_on: keycloak: condition: service_healthy`.
  3. **Missing `profile`/`email` default client scopes.** A bare `--import-realm` doesn't
     create Keycloak's usual built-in ones the way the admin console's "Create realm"
     flow does, so `preferred_username`/`email` never reached a token.
  4. **`varchar(255)` limit on `clientScopes[].description`.** Exceeding it fails the
     Liquibase migration outright with a batch-update SQL error, taking the whole import
     down with it.
  5. **Missing `requiredActions` provider registry.** A bare import creates none of the
     ~11 built-in entries (`CONFIGURE_TOTP`, `UPDATE_PASSWORD`, `VERIFY_EMAIL`, etc.), so
     Keycloak can't resolve required actions during login at all
     (`invalid_grant: "Account is not fully set up"`, event log
     `error="resolve_required_actions"`) -- pulled the authoritative provider list
     directly from a live instance's `master` realm rather than hand-guessing the schema.
     Necessary, but on its own not sufficient -- see #6.
  6. **Missing `firstName`/`lastName` on seeded users.** Keycloak's `VERIFY_PROFILE`
     required action (enabled via #5's fix) dynamically enforces the realm's User
     Profile schema at login time -- which marks these fields required by default --
     regardless of what's in the user's *stored* `requiredActions` list, which is why it
     stayed invisible through every API/admin-console check of that field. Found by
     differential debugging a real login against a working, admin-console-created test
     user: ruled out credentials (reset via the same admin API path the working user
     went through -- still failed) and every custom attribute (cleared entirely -- still
     failed) before landing on this.
  7. **Missing `aud` (audience) claim.** Keycloak does not automatically include the
     requesting client in a token's `aud` claim -- that requires an explicit "Audience"
     protocol mapper (`oidc-audience-mapper`), which nothing in the original realm
     export defined. `ingestion-api`/`orchestration-mcp` validate `audience=rag-app`
     (`common/claims.py`), so every real token failed with
     `invalid token: Token is missing the "aud" claim` -- invisible until now because
     `OIDC_SKIP_VERIFY=true` (used for every prior test this session, including all of
     #1-6's verification) never exercises audience validation at all, only real
     signature-verified tokens do. Added the mapper to the shared `nexus-rag-claims`
     client scope, verified live against the running realm before committing it.
  8. **Missing `sub` (subject) claim -- absent, not just unverifiable.** Fixing #7
     immediately surfaced this one: `ingestion-api`'s own claims parsing then crashed
     with `KeyError: 'sub'` -- not a validation error, the decoded token payload
     genuinely had no `sub` field at all. Unlike most standard claims, `sub` isn't part
     of a JWT's intrinsic structure Keycloak always includes; it's added by a mapper
     (`oidc-sub-mapper`) inside a *different* built-in scope, `basic`, distinct from
     `profile`/`email` and never referenced anywhere in the original realm export at
     all -- same "bare `--import-realm` skips built-in defaults" pattern as #3, just a
     scope we hadn't found yet. Confirmed via web search this is a
     [known](https://github.com/keycloak/keycloak/issues/31082)
     [class](https://github.com/keycloak/keycloak/issues/41098) of Keycloak issue, not
     unique to us. Pulled `basic`'s exact mapper definitions from a live instance's
     `master` realm (same technique as #5), verified live against the running realm
     (creating the scope and assigning it via the Admin API, confirming a fresh token
     carried `sub`) before committing it to `nexus-rag-claims`'s sibling scope list.
- **Browser OIDC Authorization Code + PKCE login for the ingestion UI (ARCHITECTURE.md
  Section 4.4)** — replaces the old paste-a-token workaround. The login redirect itself is
  confirmed working against a real `docker compose up`, not just the sandbox's
  `TestClient`-level verification. "Log in" redirects to Keycloak; the callback
  (`app/routes/auth.py`) exchanges the code for tokens server-to-server and stores them in
  a new `user_sessions` Postgres row, keyed by an opaque session ID in an `HttpOnly` cookie
  (never the token itself in browser-reachable storage). `app/deps.get_current_user`
  resolves that cookie to the same `UserClaims` as the header-based bearer-token path used
  by curl/API/MCP callers — transparently refreshing an expired access token via the
  stored refresh token — so no enforcement logic forks between the two. "Log out" performs
  a real Keycloak RP-initiated logout (`id_token_hint` + `post_logout_redirect_uri`), not
  just a local session clear, so logging back in re-prompts for credentials — this part and
  the nav bar's logged-in-username display (`get_current_user_optional`, used by the three
  page routes) are sandbox-`TestClient`-verified only so far, not yet run against a real
  Keycloak. See "Stubbed / TODO" below for what's still Compose-only.
- **CSRF protection on cookie-authenticated routes (NFR-14)** — a double-submit cookie
  (`nexus_rag_csrf`, set alongside the session cookie at login, deliberately *not*
  `HttpOnly` so the page's own JS can read and echo it) checked against an `X-CSRF-Token`
  header (`app/deps.verify_csrf`) on every state-changing route: document submission,
  curation approve/reject, notification read, and the admin classification/releasability
  endpoints. Only enforced when a session cookie is present — a bearer-token caller (curl,
  MCP) is never CSRF-exposed and skips this check entirely, same reasoning as
  `get_current_user`'s two paths never forking enforcement logic. Sandbox-`TestClient`-
  verified (mismatched/missing header rejected, matching header passes, bearer-token
  callers unaffected, logout clears both cookies) but not yet run against a real browser.
- **Qdrant access control (NFR-15)** — Qdrant now requires an API key in every
  environment, including this dev stack (`QDRANT__SERVICE__API_KEY` /
  `QDRANT__SERVICE__READ_ONLY_API_KEY` in `docker-compose.yml`, `.env.example`'s
  `QDRANT_API_KEY`/`QDRANT_READ_ONLY_API_KEY`). `ingestion-api` gets the full read/write
  key (it creates the collection and writes/deletes points); `orchestration-mcp` gets the
  read-only key (it only ever calls `query_points`) — least-privilege split, not just "one
  shared secret." Qdrant's host port binding also moved to `127.0.0.1:6333:6333` (was
  `6333:6333`) — defense in depth alongside the key requirement, doesn't affect
  container-to-container traffic on `nexus-rag-net`. `common/qdrant_store.py`'s
  `get_qdrant_client()` passes whatever `QDRANT_API_KEY` is in its own environment; if
  unset, the client just doesn't send the header (so this degrades gracefully against an
  unconfigured/older Qdrant rather than hard-failing, though every deployment this repo
  ships — Compose and Helm — now sets it).
- **Pinned image/model versions (NFR-16)** — every `:latest`, `main-latest`, or bare
  major-version image tag in `docker-compose.yml` and the Helm chart's `values.yaml` is now
  a specific, researched-as-current-at-pin-time release (`postgres:16.14`,
  `qdrant/qdrant:v1.18.2`, `keycloak:26.7.0`, `ollama/ollama:0.32.1`, `mongo:7.0.31`,
  `litellm:v1.93.0`) — except `librechat:v0.8.7`, deliberately held at the exact version
  Section 7.7's OBO integration recipe was verified against rather than bumped to newest.
  The three first-party images (`ingestion-api`, `orchestration-mcp`, `reranker-service`)
  in `values.yaml` are pinned to `0.1.0` (matching `Chart.yaml`'s `appVersion`) as a
  placeholder for a versioning convention, not a value backed by an actual tagged image
  yet — there's no CI pipeline in this repo that builds/pushes one. The Keycloak bump in
  particular (26.2 → 26.7.0) deserves a full `down -v` / `up` / realm-import / login retest
  before trusting it, given how many of the eight Keycloak bugs above turned out to be
  version-behavior surprises rather than code bugs.
- **Separate DB credentials for the app and Keycloak, and an append-only audit log
  (NFR-2/NFR-3)** — `POSTGRES_USER` is now the bootstrap superuser only, never used for
  day-to-day traffic. `infra/postgres/init-app-roles.sh` (runs automatically on the
  `postgres` container's first boot, via Postgres's own `docker-entrypoint-initdb.d`)
  creates two non-superuser roles: `APP_DB_USER` (`ingestion-api`/`orchestration-mcp`'s
  `DATABASE_URL`, on the existing app database) and `KEYCLOAK_DB_USER` (Keycloak's
  `KC_DB_URL`, on its own separate `KEYCLOAK_DB_NAME` database) — the app and Keycloak no
  longer share a database or credentials, in this dev stack same as production always
  required (Helm never put them on the same Postgres instance to begin with, since
  Keycloak is external there). A new one-shot `harden-audit-log` service, gated on
  `ingestion-api: condition: service_healthy` (so `audit_log` definitely exists by then --
  it's created by `common/db.py`'s `init_db()` during that service's own startup),
  reassigns `audit_log`'s ownership away from `APP_DB_USER` entirely and grants it only
  `SELECT, INSERT` — not just a `REVOKE` while `APP_DB_USER` remains the owner, which it
  could trivially undo (table owners always retain `GRANT` on their own objects; losing
  ownership outright is what actually closes that). Confirmed nothing in the codebase ever
  issues an `UPDATE`/`DELETE` against `AuditLogEntry` rows (grepped for it) before revoking
  those privileges, so this shouldn't break anything that was working. **Not tested
  live** — this is the riskiest change in this hardening batch (unlike the others, a
  mistake here could break every DB-touching code path in both services, not just degrade
  one feature), and deserves a full `docker compose down -v && docker compose up` pass with
  close attention to whether `ingestion-api`/`orchestration-mcp`/`keycloak` actually come up
  healthy before relying on it.
- **Search page in the ingestion UI (http://localhost:8001/search)** — a query-testing
  page for a logged-in user, proxying to `orchestration-mcp`'s existing `/debug/rag_search`
  REST endpoint (`app/routes/search.py`) with the session's own access token forwarded
  unchanged. No enforcement logic duplicated here — `orchestration-mcp` (FR-24..FR-29)
  still does all of it, including the `rag-query` role check; this route just resolves
  "what's the current user's token" and passes the response through, same access filter a
  real LibreChat query would get. Not a LibreChat replacement, just a faster way to test a
  query than curl.
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
- **Keycloak OBO/token-exchange, still needs manual admin-console steps.** The `rag-app`
  client's `standard.token.exchange.enabled: "true"` attribute (in the realm export) marks
  it as an OBO exchange target (Section 7.7), but Keycloak 26.2+ also requires a
  fine-grained admin permission granting the `librechat` client permission to actually
  exchange for `rag-app`'s tokens — that policy isn't expressible in a plain realm-export
  JSON at all (note: don't add a `_comment` field or similar to work around that akin to
  what a `.json`/`.yaml` comment would do — Keycloak's realm importer uses strict JSON
  deserialization and will refuse to import the whole realm over a single unrecognized
  property, which is exactly what broke `--import-realm` before this was caught during a
  real `docker compose up` run: `ERROR: Unrecognized field "_comment"`). Finish this in the
  admin console after import. Similarly, reusable access tokens (the other Section 7.7 OBO
  prerequisite) are a LibreChat-side OpenID setting, not a Keycloak client attribute — set
  via `OPENID_REUSE_TOKENS=true` in `docker-compose.yml`'s `librechat` service environment,
  not `librechat.yaml` (that file is LibreChat's `endpoints`/`mcpServers` config, not its
  auth environment variables).
- `infra/librechat/librechat.yaml`'s exact schema hasn't been validated against a running
  LibreChat 0.8.7 instance (only `orchestration-mcp`'s side of the OBO connection has been
  verified, using the real MCP client SDK standing in for LibreChat's MCP client).
- **Helm chart changes are hand-written, unverified by `helm lint`/`helm template`** — no
  network access to install the `helm` CLI in this environment (see
  `helm/nexus-rag/README.md`'s note at the top, unchanged from earlier chart work). This
  applies to the new `externalKeycloak.clientId`/`clientSecret` and
  `ingestionApi.oidcRedirectUri`/`cookieSecure` wiring same as everything else in the
  chart — run `helm template --debug` against a real values override before trusting it.

## Resetting

```bash
docker compose down -v   # also wipes Postgres/Qdrant/Ollama/reranker-cache volumes
```
