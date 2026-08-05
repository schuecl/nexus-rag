# Local Dev Environment (NFR-9)

One-command stand-up of the nexus-rag stack for exercising the ingest → curate → query
flow on a workstation, with zero dependency on the production cluster. Every service is
wired together, the auth/tagging plumbing works end to end, submitted documents are
durably queued and parsed, chunked, embedded, and made retrievable once approved
(FR-3..FR-6, NFR-11), retrieval genuinely fuses dense+BM25 hybrid search with a reranking
pass (FR-24/FR-25), documents can be versioned (FR-7), and `orchestration-mcp`'s MCP tool
reads the caller's identity from the connection's Authorization header rather than a
client-supplied argument. **Confirmed against a real `docker compose up`** (not just
inspected as code) end to end, across two separate live-testing rounds: an earlier one
against the pre-NATS pipeline (see the Keycloak realm bullet below for that round's eight
real bugs), and a second one after NFR-11 restructured ingestion around a durable NATS
queue and a new `ingestion-worker` service — upload through `ingestion-api` with a real
Keycloak-obtained token, durable queuing and processing, curation, and a claims-filtered
query all manually verified working end to end, catching and fixing a real object-store
permission bug (see the NFR-11 bullet below) along the way. **LibreChat's own OIDC login is
now confirmed working end to end (issue #75)** — several real LibreChat config bugs were
found and fixed chasing it (see the `ALLOW_SOCIAL_LOGIN`/`OPENID_SCOPE`/MCP-allowlist
bullets below), and the actual root cause (`openid-client` refusing a plain-HTTP issuer)
needed a real HTTPS setup, not just config — see "One-time host setup" below.

**The MCP tool-calling path is now confirmed fully working end to end (issue #99
follow-up, 2026-07-26)** — `bob-query` driving `rag_search` through a real LibreChat Agent
returns real, claims-filtered search results. Getting there took six real bugs, in order:

1. **Wrong OBO grant type.** LibreChat's actual `OboTokenService` calls Keycloak with
   `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` (RFC 7523), not
   `grant_type=token-exchange` (RFC 8693) as the realm's `standard.token.exchange.enabled`
   attribute was configured and manually verified for — the earlier scripted exchange
   replicated the wrong grant type entirely.
2. **`addUserJwtToken: true` (the interim fix that replaced OBO) never actually worked.**
   It looked plausible from reading LibreChat's docs/code, but a live `tcpdump` capture on
   `orchestration-mcp`'s own port showed zero `Authorization` header on the real MCP
   request. Grepped the installed LibreChat build's actual Zod schema
   (`StreamableHTTPOptionsSchema` in `packages/data-provider`) and confirmed
   `addUserJwtToken` isn't a recognized field in this LibreChat version at all — unknown
   keys are silently dropped, so this had been a no-op since it was first added, not a
   mechanism that broke later.
3. **RFC 7523 (`obo`) genuinely doesn't fit this setup, confirmed against Keycloak's own
   docs** (not assumption, see the Keycloak OBO bullet below): it requires the assertion's
   issuer to be a registered, *linked external* Identity Provider, and the assertion's
   `aud` to equal Keycloak's own issuer/token-endpoint URL — neither holds for `librechat`
   and `rag-app` being two clients in the same realm on the same Keycloak instance. Checked
   whether a different self-hosted IdP (Authentik, Ory Hydra, Zitadel) would sidestep this:
   no, because LibreChat's OBO code always sends `grant_type=jwt-bearer` regardless of
   which IdP is behind it — swapping IdPs doesn't change what LibreChat asks for, and
   replacing Keycloak everywhere in this stack for an OBO nuance would be a full
   infrastructure migration, not a fix.
4. **Switched to MCP OAuth login instead of OBO/token-forwarding** —
   `infra/librechat/librechat.yaml`'s `rag` server now uses a real
   `oauth`/`requiresOAuth: true` config (RFC 6749 `authorization_code` against Keycloak
   directly, reusing the existing `rag-app` client) rather than trying to silently forward
   or exchange the user's existing LibreChat-login token. This is genuinely different from
   OBO, not a rename of the same problem: it's a standard browser login/consent flow
   LibreChat itself drives (the user clicks "Connect" once per MCP server), so the token
   that comes back is issued by Keycloak the ordinary way and passes `orchestration-mcp`'s
   existing signature/audience check unmodified — no same-realm jwt-bearer trust
   relationship needed at all.
5. **Two more bugs surfaced getting the OAuth login itself to actually trigger and
   complete**, both found via live logs, not guessing: `requiresOAuth` needed to be forced
   to `true` explicitly because the original MCP transport did not issue an authentication
   challenge; and the Keycloak authorization URL was rejected as resolving to a private IP
   address even after adding it to `mcpSettings.allowedAddresses` (the correct-looking
   SSRF-exemption field) — reading `packages/api`'s actual `isOAuthUrlAllowed()` showed
   that once `allowedDomains` is non-empty (ours already was, for the orchestration-mcp
   entry), it becomes the *sole* authority for this check and `allowedAddresses` is ignored
   outright, by design. Fixed by adding Keycloak to `allowedDomains` instead. See the
   `oauth`/`requiresOAuth`/`allowedDomains` bullets below for the full details on each.
6. **The MCP connection retained an expired 15-minute Keycloak bearer** (issue #200,
   2026-07-28). JWT verification previously happened only inside `rag_search`, which
   returned `{"error":"invalid token: Signature has expired"}` as a successful HTTP 200
   tool result. LibreChat therefore had no OAuth failure signal and did not redeem its
   refresh token. `orchestration-mcp` now verifies the bearer at the MCP transport
   boundary and returns RFC 6750 `401 invalid_token`; LibreChat can refresh, reconnect, and
   retry. `requiresOAuth: true` remains explicit, and disconnect now uses Keycloak's real
   revocation endpoint instead of the MCP server's nonexistent `/revoke` route.

Also fixed along the way, independent of the auth-forwarding saga above: a `421 Invalid
Host header` MCP transport bug (see the `transport_security`/`TransportSecuritySettings`
bullet), the fact that a bare chat via the `LiteLLM`/`Ollama-Direct` custom endpoints never
calls MCP tools at all (Agents-only, by LibreChat design, not a bug), swapping
`GENERATION_MODEL` from `llama3.2:1b` to `qwen2.5:7b-instruct` for reliable tool-calling,
and shortening `rag_search`'s docstring and the MCP server's config key after discovering
even `qwen2.5:7b-instruct` failed against the real (long, namespaced) MCP-served schema —
see those bullets below for the full before/after evidence.
See "What's stubbed vs working" below for the complete, current list.

**Schema note:** this version writes chunks with two named Qdrant vectors (`dense` +
`bm25`) instead of one unnamed vector, and (issue #229) into one collection per
Classification level instead of a single shared `nexus_rag_chunks`. If you have a Qdrant
volume from before either change, run `docker compose down -v` first -- `ensure_collection`
only configures a collection when it doesn't already exist, so a stale volume won't pick up
the new schema, or get split into the new per-classification collections, on its own.

## Running on a GPU host (optional)

Everything defaults to CPU so `docker compose up` works with no drivers. Two
pieces can use an NVIDIA GPU when one is present:

- **reranker-service** bakes its torch wheel at build time from
  `TORCH_INDEX_URL` (`.env`). CPU by default; for GPU set it to the matching
  CUDA index (e.g. `https://download.pytorch.org/whl/cu124`) and uncomment the
  service's `deploy.resources` GPU reservation in `docker-compose.yml`.
- **Ollama** uses the GPU automatically once its GPU reservation is uncommented.

Host prerequisites for the GPU path: an NVIDIA driver, the
`nvidia-container-toolkit`, and Docker configured with the `nvidia` runtime.
Air-gapped (NFR-1): mirror the chosen torch index internally and point
`TORCH_INDEX_URL` at it, same as PyPI is already mirrored.
## Image/figure captioning (optional, #92)

Off by default. When enabled, `ingestion-worker` extracts embedded images from
PDF/DOCX/PPTX at ingestion, captions each with a vision model on the stack's
existing Ollama, and stores every caption as its own retrievable chunk
(`content_type: "image"`, issue #89's tagging — so `CONTENT_TYPE_BOOSTS`
can weight figure content at query time).

Enable by setting a vision-capable Ollama model in `.env`:

```bash
VISION_MODEL=moondream        # ~1.7GB, usable on CPU
# or granite3.2-vision       # ~2.4GB, stronger on charts/document figures
```

`ollama-model-init` pulls the model on the next `up` (needs internet once,
NFR-1: mirror it internally like the other models). Leaving `VISION_MODEL`
empty keeps ingestion byte-identical to today: no pull, no VLM calls.

Failure semantics are degrade-not-fail (the reranker pattern, not
`ParsingError`'s): a down/missing model, a per-image error, or the captioning
pass outrunning its budget (`CAPTIONING_TIMEOUT_SECONDS`, default 90s) costs
captions, never the document — the gap is visible in the
`nexus_rag_ingestion_worker_images_skipped_total{reason=...}` counter rather
than in a failed ingestion. Glyph/logo-sized images are filtered
(`CAPTION_MIN_IMAGE_BYTES`/`CAPTION_MIN_IMAGE_DIMENSION`), repeats are
deduplicated, and `MAX_IMAGES_PER_DOCUMENT` (default 20) bounds the model
calls per document.

Status: extraction/captioning/degrade paths are tested against mocks
(`services/ingestion-worker/tests/test_captioning.py`; respx-mocked Ollama,
in-memory fixture documents); the enabled path is validated against a live
environment (a real `docker compose up` with `VISION_MODEL=moondream`: a
PPTX with an embedded chart ingested, captioned, curator-approved, and the
caption chunk retrieved through `/debug/rag_search`).
## LLM classification suggestion (optional, #308)

Off by default. When enabled, `ingestion-worker` asks a text-generation model on the
stack's existing Ollama to zero-shot classify a document -- a Classification value
matched against the admin-configured `ClassificationLevel` list, plus a free-text
doc_type/program_community guess, a confidence score, and a short rationale. Folded
into the same `document.tagging_advisory` JSON column and `/curate` advisory box
Phase 1/2 (#138, #307) use, only surfaced when it disagrees with the assigned tags --
same "only speak up when something's off" gating.

Enable by setting a generation-capable Ollama model in `.env`:

```bash
CLASSIFICATION_MODEL=qwen2.5:3b-instruct   # GENERATION_MODEL's value works -- already pulled
```

`ollama-model-init` pulls the model on the next `up` (needs internet once, NFR-1:
mirror it internally like the other models). Leaving `CLASSIFICATION_MODEL` empty keeps
ingestion byte-identical to today: no pull, no extra LLM calls.

Failure semantics are degrade-not-fail, same posture as captioning: a down/missing
model or a malformed/non-JSON response costs the suggestion, never the document -- the
gap is visible in the `nexus_rag_ingestion_worker_llm_suggestions_total{outcome=...}`
counter rather than a failed ingestion. A suggested Classification value outside the
configured list is dropped, never invented (`app/classification_suggestion.py`); only
an *under*-classification (suggested rank higher than assigned) is flagged, matching
Phase 1/2's asymmetric semantics, while any doc_type difference is flagged regardless
of direction since it carries no spillage-direction concern the way Classification
does.

Scope note: only Classification is an admin-configurable, DB-backed controlled list
today (`ClassificationLevel`) -- `doc_type`/`program_community` have no equivalent
table (Section 6.3 itself only ever calls program_community "free-form OR
controlled"), so the model is asked to *match* Classification against the real
configured list but only to *guess* doc_type/program_community in its own words.
Adding real controlled lists for those two fields is a natural follow-up, not part of
this phase.

Status: unit-level behavior is tested against mocks (`services/ingestion-worker/tests/
test_classification_suggestion.py`, respx-mocked Ollama; `test_llm_suggestion_advisory.py`,
worker glue against an in-memory SQLite session; `services/ingestion-api/tests/
test_tagging_advisory_linkage.py`, curator-decision audit linkage). **Validated against a
live environment**: real `docker compose up` (`CLASSIFICATION_MODEL=qwen2.5:3b-instruct`,
the model already pulled for `GENERATION_MODEL`), a document worded to read as sensitive
content without any literal CAPCO/DoD banner string (so Phase 1's marking-mismatch
detector stays clean) uploaded through the real `POST /documents` API tagged
`CUI`/`report`, confirmed end to end: the worker's real Ollama call returned
`classification=SECRET, doc_type="briefing slide", confidence=0.95` with a substantive
rationale, `GET /curate/queue` surfaced it alongside Phase 1 (clean) and Phase 2
(agreeing) in the same advisory object, approving it recorded
`llm_suggested_classification`/`llm_suggested_doc_type`/`llm_confidence` in the
`document.approve` audit entry (confirmed via direct DB read for diagnostic purposes --
ingestion-api's own audit_log grant is INSERT-only, `docs/roles-and-permissions.md`), and
`nexus_rag_ingestion_worker_llm_suggestions_total{outcome="disagrees"}` incremented on
`/metrics`. A worker crash-loop from a stale `postgres-data` volume's per-service role
grants predating this run was hit and resolved by re-running the existing
`ensure-db-roles`/`lock-down-db-grants` one-shot jobs (both already designed to be
idempotent and safe to re-run on any `up`, per their own comments in
`docker-compose.yml`) -- unrelated to this feature, not a new gap it introduced.
## OCR for scanned and image content (#241)

Always on -- OCR is parsing, not an optional enrichment, and it involves no
network call: Tesseract is baked into the `ingestion-worker` image (eng
traineddata; set `OCR_LANG` and add the matching Debian package to the
Dockerfile for other languages -- never downloaded at runtime, NFR-1).

Two paths use it:

- **Image uploads** (`.png`/`.jpg`/`.jpeg`/`.tif`/`.tiff`, new in the #211
  allowlist): the recognized text becomes the document's content. No
  recognizable text is an actionable FR-8 failure ("no readable text was
  recognized"), not an empty success.
- **Scanned PDF pages**: a per-page fallback that fires only when the page
  yielded no prose and no tables -- a PDF with a text layer parses
  byte-identically to before. A missing/broken tesseract here degrades to a
  logged skip (those pages contributed nothing before #241 either).

OCR-derived chunks carry `content_type: "ocr"` (issue #89) -- visible to
curators as machine-read provenance and weightable via `CONTENT_TYPE_BOOSTS`.
Unlike `table`/`image` sections they are chunked by the normal sliding
window. Work is bounded by `MAX_OCR_IMAGES_PER_DOCUMENT` (default 50),
`OCR_MIN_IMAGE_DIMENSION`, and #208's per-document processing timeout.

Status: parse/degrade/failure paths are tested against mocks
(`services/ingestion-worker/tests/test_ocr_parsing.py`; stubbed pytesseract,
in-memory fixtures); the end-to-end path is validated against a live
environment (a real `docker compose up` build with tesseract in the image: a
rendered-text scanned PDF and a PNG memo ingested, approved, and retrieved
via `/debug/rag_search` by querying for their pixel-only text).

## Container hardening (#111)

Every Compose service runs with `security_opt: ["no-new-privileges:true"]`, and
the four custom-built services mirror the Helm chart's `securityContext`
exactly:

| setting | Compose | chart (`_helpers.tpl`) |
|---|---|---|
| non-root uid | `user: "10001:10001"` | `runAsUser: 10001` |
| read-only rootfs | `read_only: true` + `tmpfs: [/tmp:size=64m]` | `readOnlyRootFilesystem: true` + `emptyDir` |
| capabilities | `cap_drop: ["ALL"]` | `capabilities.drop: ["ALL"]` |
| privilege escalation | `no-new-privileges:true` | `allowPrivilegeEscalation: false` |

The point is that the settings gating production get exercised on every dev run
and every `e2e.yml` run. `read_only` in particular is the kind of thing that
works until some library wants a scratch path — better to find that here.

**The `tmpfs` mount carries an explicit size (#209), unlike the table above
suggests at a glance.** Compose's `tmpfs` is RAM-backed; Kubernetes' `emptyDir`
is not automatically, but the chart already bounds it via the pod's
`ephemeral-storage` resource limit. An unsized Compose `tmpfs` had no
equivalent bound: Starlette's multipart parser spools an upload past 1MB to a
temp file on disk *before* the route ever runs `_read_bounded`'s
`MAX_UPLOAD_BYTES` check (`services/ingestion-api/app/routes/upload.py`), and
in Compose that temp file lands on this RAM-backed mount. `size=64m` is
headroom above legitimate scratch use, not a sizing of the largest accepted
upload — the 50MB `MAX_UPLOAD_BYTES` check still runs after the parser hands
control back, this only bounds what the parser itself can do first. Helm's
equivalent bound is the ingress `proxy-body-size` annotation (#107) — Compose
has no proxy in front of `ingestion-api`, so this is the closest available
substitute, not a like-for-like replacement.

**FR-34/#356's `POST /documents/batch` compounds this gap**: a batch sends every
file as one multipart request, so the bytes the parser spools before any
`MAX_UPLOAD_BYTES`/`MAX_BATCH_FILES` check runs scale with the whole batch, not
one file. A batch whose combined size clears `size=64m` fails with a generic
`400 "There was an error parsing the body"` rather than a clear per-file error
-- reproduced locally with three 25MB files. The Helm chart's ingress template
now sizes `proxy-body-size` as `maxBatchFiles x maxUploadBytes` for exactly
this reason (`templates/ingestion-api-ingress.yaml`); Compose's `tmpfs` has no
equivalent per-request scaling and is left at its single-file-sized `64m`,
since bumping a RAM-backed dev mount to match the full batch ceiling
(1.25GB at the chart's defaults) isn't worth the tradeoff for a local loop --
keep batches modest in Compose, or raise `size=` yourself if you need to
exercise a larger one.

`cap_drop: ["ALL"]` also applies to Qdrant, NATS, and Ollama. Postgres and
Keycloak keep their default capability set: both drop privileges from root at
startup and need `CAP_CHOWN`/`SETUID`/`SETGID` to do it.

`scripts/check_compose_hardening.py` enforces all of this in CI, alongside the
NFR-16 pinning check, so the two definitions can't drift apart silently.

**Every published port binds `127.0.0.1` explicitly.** A bare `8003:8003`
listens on all interfaces, which on a laptop on a shared network makes
`reranker-service` (which sees retrieved chunk text) and `ollama` open
unauthenticated endpoints. Containers reach each other by service name on
`nexus-rag-net` regardless, so nothing in the documented flow changes — the
`http://localhost:PORT` URLs below all still work.

## Observability stack (optional, #133)

Off by default. Bring it up alongside the app stack:

```bash
docker compose --profile observability up -d
```

**The profile alone does not enable tracing, JSON logs, or profiling.**
Uncomment these in `.env` first, or Tempo/Pyroscope stay empty and the
trace-to-logs and trace-to-profiles links have nothing to match on:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
LOG_FORMAT=json
PYROSCOPE_SERVER_ADDRESS=http://pyroscope:4040
```

Put them in `.env` rather than inline on the `up` command — an inline variable
is lost as soon as a container is recreated from Docker Desktop or a plain
`docker compose up`, and the only symptom is a `tracing disabled` /
`profiling disabled` line at startup that is easy to scroll past.

- **Grafana** http://localhost:3000 (`admin` / `nexus-rag-admin`) -- dashboards
  and datasources are auto-provisioned (Prometheus, Loki, Tempo, Pyroscope).
- **Prometheus** http://127.0.0.1:9090 scrapes every service's `/metrics`.
- **Tempo** (traces) and **Loki** (logs) are wired as Grafana datasources.
- **Pyroscope** http://127.0.0.1:4040 (continuous CPU profiling, #349) is
  wired as a Grafana datasource. All four services (`ingestion-api`,
  `ingestion-worker`, `orchestration-mcp`, `reranker-service`) push it
  100Hz CPU samples once `PYROSCOPE_SERVER_ADDRESS` is set -- continuous,
  not triggered, the same always-on posture Prometheus and Tempo already
  have in this stack. Memory profiling is off (nothing has asked for heap
  data yet).
- Every port except Grafana is published loopback-only.
- **Trace-to-log correlation works**: with `LOG_FORMAT=json` and tracing
  enabled, every log line carries `trace_id`/`span_id`, which is what
  Grafana's provisioned Loki datasource matches on to jump between a log line
  and its trace in Tempo.
- **Grafana's service map is populated** by Tempo's metrics-generator, which
  derives span metrics and service graphs as spans arrive and remote-writes
  them to Prometheus (`--web.enable-remote-write-receiver`).

### What holds the Docker socket, and why

Alloy discovers containers and tails their stdout through
**docker-socket-proxy**, not through a direct `/var/run/docker.sock` mount. A
`:ro` bind on the socket restricts the file, not the daemon API behind it —
anything that can talk to the socket can create a privileged container, so it
is root on the host. The proxy allows only the `GET /containers` endpoints
Alloy needs and refuses container creation and exec.

This reduces the exposure, it does not remove it: the proxy itself holds the
socket. It is a dev-only profile, never started by a bare `docker compose up`,
and the Helm chart has no equivalent (pods log to the node's collector).

### Postgres metrics

`postgres-exporter` scrapes as `MONITORING_DB_USER` (`nexus_rag_monitor` by
default), a dedicated role with `pg_monitor` and `CONNECT` and no table
privileges. Not `APP_DB_USER`: most of the exporter's collectors return
nothing without `pg_monitor`, and granting it to the application credential
would give the whole app cluster-wide read of every session's queries, which
is the separation NFR-3 exists to keep. The role is created by
`infra/postgres/init-app-roles.sh`, which runs **once**, on the first boot of a
fresh data directory — on a pre-existing volume, create it by hand or recreate
the volume.

### Kubernetes

The `nexus-rag` chart does not deploy a monitoring stack; a cluster inside the
accreditation boundary already runs one. Set
`observability.serviceMonitor.enabled=true` (off by default, since rendering a
ServiceMonitor without the Prometheus Operator's CRDs fails the install) to
let that stack discover the services' `/metrics` endpoints — and allow the
monitoring namespace through the chart's default-deny NetworkPolicies, which
otherwise block the scrape. **This remains the preferred arrangement.**

For a cluster that has *no* monitoring stack, and whose Grafana runs outside it
on the air-gapped network, there is a second, separately-installed chart:
`helm/observability` (#257) deploys Prometheus/Loki/Tempo/Alertmanager on
LoadBalancer addresses, adds a ClusterIP-only Pushgateway for sanitized
Q-to-C-to-A batch metrics, vendors the 14 dashboards for import, and deploys no
Grafana. See [observability.md](observability.md).

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

## Restrictive host umask (issue #192)

If your host's default `umask` is restrictive (e.g. `077`, common on hardened/RHEL
workstations), a fresh checkout used to create every git-tracked config file `0600`/`0700`
(git only preserves the executable bit; the rest comes from `0666`/`0777 & ~umask`) --
unreadable by the non-root users several bind-mounted images run as (Postgres, nats, the
Prometheus/Grafana-stack images, Keycloak). This no longer needs a manual step: the
`fix-config-perms` one-shot service normalizes permissions across `infra/` (except
`infra/certs`, whose private keys are deliberately kept restrictive and are generated at
runtime, not checked out from git) on every `docker compose up`, before any dependent
service starts.

## One-time host setup for LibreChat OIDC (issue #75)

LibreChat's own OIDC login needs two things a fresh checkout doesn't have yet:

1. **A trusted dev CA + cert.** LibreChat's `openid-client` refuses a plain-HTTP
   `OPENID_ISSUER` outright, and LibreChat v0.8.7 has no built-in HTTPS listener of its
   own, so both Keycloak and a small nginx proxy in front of LibreChat need a real
   (self-signed, dev-only) cert:
   ```bash
   infra/certs/generate-dev-certs.sh
   ```
   Then trust `infra/certs/ca.crt`:
   - **Browser** (Firefox/Chrome, NSS-backed, no sudo): `certutil -A -d "sql:$HOME/.mozilla/firefox/<profile>" -n "nexus-rag dev CA" -t "C,," -i infra/certs/ca.crt` (repeat per Firefox profile; Chrome/Chromium share `sql:$HOME/.pki/nssdb`).
   - **System store** (only needed if you'll `curl`/script against these endpoints from the host): `sudo cp infra/certs/ca.crt /etc/pki/ca-trust/source/anchors/nexus-rag-dev-ca.crt && sudo update-ca-trust` (Fedora/RHEL-family; Debian/Ubuntu use `/usr/local/share/ca-certificates/` + `sudo update-ca-certificates`).
2. **A `/etc/hosts` alias for `keycloak`.** Keycloak's discovery metadata (including the
   browser-facing `authorization_endpoint`, not just LibreChat's own backend calls) is
   stamped with whichever hostname the request used (`start-dev`'s default, request-based
   behavior — see the dual-issuer note below). LibreChat's backend reaches Keycloak over
   the Compose network as `keycloak`, so the browser needs that same name to resolve too:
   ```bash
   echo "127.0.0.1 keycloak" | sudo tee -a /etc/hosts
   ```

Skip either step and the OIDC login redirect will fail — a missing CA trust shows up as a
TLS error in LibreChat's logs or a browser cert warning at the Keycloak redirect; a missing
`/etc/hosts` entry shows up as the browser failing to resolve `keycloak` after login.

## Start the stack

```bash
cp .env.example .env
docker compose up --build
```

(This builds from source, which is what dev wants. Deploying a specific
*released* version — versioned images, the packaged chart, the air-gapped
import flow — is a different path: see [`releasing.md`](releasing.md), #295.)

First boot takes a while: Keycloak imports the realm, `ollama-model-init` pulls
`nomic-embed-text` and `llama3.2:1b`, `reranker-service` downloads
`cross-encoder/ms-marco-MiniLM-L6-v2`, `ingestion-api`/`orchestration-mcp` each download
the tiny (~10MB) `Qdrant/bm25` sparse model on first use — all from Hugging Face — and
finally `seed-sample-data` submits and curates 7 sample documents through the real API
once everything above is healthy.

Both HF model downloads are pinned to a commit revision (issue #210), not just a
mutable model name, and enforced in CI by `scripts/check_pinned_models.py`:
`cross-encoder/ms-marco-MiniLM-L6-v2` resolves to `c5ee24cb16019beea0893ab7796b1df96625c6b8`
(`RERANKER_MODEL_REVISION`, reranker-service), `Qdrant/bm25` to
`e499a1f8d6bec960aab5533a0941bf914e70faf9` (`BM25_MODEL_REVISION`, shared by
`ingestion-worker`/`orchestration-mcp` via `services/common`). An air-gapped deployment
mirroring these models internally should mirror exactly these revisions.

| Service | URL | Notes |
|---|---|---|
| Keycloak admin console | http://localhost:8080 | login `admin` / `admin` (`.env`) |
| Keycloak health/metrics | http://localhost:9000/health/ready | `KC_HEALTH_ENABLED=true` moves `/health*` onto Keycloak's separate management interface (default port 9000) rather than 8080 -- what the `keycloak` service's Compose healthcheck actually probes |
| Ingestion UI | http://localhost:8001 | lands on a login page (issue #246); click the login button, real Keycloak login, then upload form, curation queue, and a search page |
| orchestration-mcp debug API | http://localhost:8002 | `/health`, `/debug/rag_search` |
| reranker-service | http://localhost:8003 | `/health`, `/rerank` |
| ingestion-worker | http://localhost:8004 | `/health` only -- its real work is the NATS consumer loop, not an HTTP API (NFR-11) |
| Qdrant | http://localhost:6333/dashboard | |
| Attu (Milvus profile only) | http://127.0.0.1:8000 | connect to `milvus:19530` with the dev Milvus credentials; host port is `ATTU_PORT` |
| LibreChat | https://localhost:3080 | throwaway, log in via Keycloak. HTTPS is real (issue #75) via the `librechat-proxy` nginx service, not just a config label -- see "One-time host setup" above |
| LiteLLM | http://localhost:4000 | throwaway gateway in front of Ollama |

## Seeded Keycloak users (realm `nexus-rag`)

All dev-only, password `devpass123` for every account — **never reuse these in a real
environment.**

| Username | Roles | Clearance | Releasability | Org | Purpose |
|---|---|---|---|---|---|
| `alice-ingest` | `rag-ingest` | CUI | FVEY | USAREUR-AF | ingest-only |
| `bob-query` | `rag-query` | SECRET | FVEY, NATO | USAREUR-AF | query-only |
| `carol-curator` | `rag-query`, `rag-curate:USAREUR-AF` | SECRET | FVEY, NATO | USAREUR-AF | curator scoped to one org |
| `dave-admin` | all roles + both curator orgs | SECRET | NOFORN, USA, NATO, FVEY | USAREUR-AF | admin |
| `eve-purge` | `rag-purge` only | — | — | USAREUR-AF | second, independent purge holder |

Clearance and Releasability are both granted via `rag-clearance:<value>` and
`rag-releasability:<value>` client roles (same convention as `rag-curate:<org>`), not user
attributes — see REQUIREMENTS.md Section 6.2.

Issue #279 (gap G3): `dave-admin` already holds `rag-purge` alongside every other role,
which collapses the separation `deps.require_purge`'s docstring argues for. `eve-purge` is
seeded specifically so the two-person purge-request/confirm flow
(`docs/roles-and-permissions.md` §7 G3) has a second, genuinely independent `rag-purge`
identity to confirm with — `PURGE_TWO_PERSON_REQUIRED` is off by default in this dev
stack (see `docker-compose.yml`), so exercising the two-person path here means setting it
to `true` yourself.

## Getting a token for API testing (dev-only password grant)

The ingestion UI's browser pages now use a real Keycloak login redirect (land on the login
page, click its button — ARCHITECTURE.md Section 4.4); this section is for curl/API
testing, which still needs a raw bearer token. Get one with:

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

### Chat plane troubleshooting (#193)

A **successful Keycloak login followed by a chat error is not an auth failure** — the
OIDC path (login, claims, `rag_search` access filtering) is independent of the
LibreChat → LiteLLM → Ollama generation path. Two symptoms seen during testing:

- **"Missing API Key for LiteLLM."** The LiteLLM endpoint's key comes from
  `LITELLM_MASTER_KEY` in the `librechat` container's environment (substituted into
  `infra/librechat/librechat.yaml`). It is wired in `docker-compose.yml`; if you see
  this, confirm `docker exec <librechat> printenv LITELLM_MASTER_KEY` is non-empty and
  matches LiteLLM's own value. It has nothing to do with the user's login.
- **`400 "nomic-embed-text:latest" does not support chat`.** The generation model
  (`qwen2.5:7b-instruct`, ~5 GB) is pulled by `ollama-model-init` on first boot, after
  the embedding model. If that pull is interrupted (no internet, or a full disk — the
  full first boot needs ~10 GB free), only the embedding model remains and the model
  list offers it for chat, which it cannot do. Re-pull it:

  ```bash
  docker exec "$(docker compose ps -q ollama)" ollama pull qwen2.5:7b-instruct
  docker exec "$(docker compose ps -q ollama)" ollama list   # confirm qwen is present
  ```

  Then pick the **LiteLLM** endpoint and the `qwen2.5:7b-instruct` model in LibreChat.

## Exercising the flow

By the time `docker compose up` finishes, `seed-sample-data` has already run steps 1-2
below for you against 7 real documents (see "What's stubbed vs working"). To query them
immediately, get a `bob-query` token (step 3's instructions) and search for e.g.
`password rotation` or `VPN access` — or skip ahead to step 3 directly. The steps below
walk through the same flow manually, useful for understanding what the seed script
automated or for testing with your own file.

1. **Submit a document** as `alice-ingest`, either through http://localhost:8001 (land on
   the login page, click its button, authenticate as `alice-ingest` at Keycloak) or
   directly:

   ```bash
   TOKEN=$(...)  # from above
   curl -s http://localhost:8001/documents \
     -H "Authorization: Bearer $TOKEN" \
     -F file=@/path/to/some.pdf \
     -F classification=CUI \
     -F 'releasability=["FVEY"]' \
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
   curl -s -X POST http://localhost:8002/debug/rag_search \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"query": "<a phrase from your doc>", "top_k": 5}'
   ```

   The query goes in the body, not the URL (#214): a question asked of a
   classified corpus is itself sensitive, which is why #125 kept it out of the
   audit log — and a `?query=` lands in every proxy and ingress access log in
   the path. The query-string form still works so existing scripts don't
   break, but it logs a warning.

   This endpoint is a development convenience. Set
   `DEBUG_RAG_SEARCH_ENABLED=false` to remove it from a deployed image;
   authorization is enforced either way, so this is about surface, not a hole.

   The UI page is a thin proxy over this same endpoint (`app/routes/search.py`), forwarding
   your logged-in session's own token — same access filter either way.

   Expect `results` to contain the matching chunk(s), each with the source document's
   `applied_filter`-passing payload (classification, releasability, access_scope,
   filename, heading/page_or_slide). `hybrid_retrieval` and `reranking` in the response
   describe what actually ran — a dense+BM25 RRF fusion over the candidate pool, then a
   cross-encoder rerank via `reranker-service` (falls back to the fused order with a note
   if `reranker-service` is unreachable, rather than failing the query). Query as a user
   outside the document's `access_scope` (e.g. someone not in `USAREUR-AF` and the doc
   isn't tagged `ALL_AUTHENTICATED`) and confirm `results` comes back empty — that's FR-26
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

## Live validation history

Most of this project was built with no Docker daemon, live Keycloak/Qdrant/Ollama, or
Hugging Face access available, so initial verification leaned on `TestClient`/`uvicorn`/
MCP-client round trips, in-memory Postgres/Qdrant, and hand-crafted JWTs — rigorous, but
not the same as a real cluster.

The full pipeline has since been validated against a real `docker compose up`, including
the P0 durability work: a document submitted as `alice-ingest` was durably queued (NATS
JetStream), picked up and processed by `ingestion-worker` (`queued → processing →
pending_review`, confirmed via `GET /documents/{id}` polling), curated and approved, and
then found by a real claims-filtered query against `orchestration-mcp`'s
`/debug/rag_search` with a `bob-query`-obtained Keycloak token.

That live run surfaced and fixed eight real bugs this repo's mocked verification couldn't
have caught:

1. A missing `mkdir` before a non-root `chown` — object-store write permissions.
2. `obo.scopes` supplied as a JSON array where LibreChat expects a space-delimited string.
3. Missing `JWT_SECRET` / `CREDS_KEY` / `CREDS_IV` in the LibreChat config.
4. `ALLOW_SOCIAL_LOGIN` defaulting off, which disabled the OIDC button.
5. An MCP SSRF domain-allowlist that blocked `orchestration-mcp`.
6. A missing `OPENID_SCOPE`.
7. A `depends_on` race against Keycloak's healthcheck.
8. `openid-client` refusing a plain-HTTP issuer, which broke LibreChat's own OIDC login
   (issue #75) — fixed with a real (self-signed, dev-only) HTTPS cert for both Keycloak
   and a small nginx proxy in front of LibreChat, which has no HTTPS listener of its own.

The OBO token-exchange mechanism itself is confirmed live: two real Keycloak config bugs
were found and fixed (the Standard Token Exchange V2 switch was on the wrong client, and
the exchanged token's HTTPS issuer wasn't in `orchestration-mcp`/`ingestion-api`'s issuer
allowlist), and a scripted exchange now returns a correctly claims-filtered `rag_search`
result.

Still not confirmed: LibreChat's *own* code performing that exchange when a real chat
message triggers the tool — driving that specific path hit a separate LibreChat auth quirk
(its `openidJwt` reused-token strategy rejects its own token with "invalid algorithm"),
tracked as a follow-up rather than chased down inline. See the "What's stubbed vs working"
list below and `REQUIREMENTS.md`'s P1 list.

**FR-34/#356's `POST /documents/batch` was validated live against a real `docker compose
up`** (curl, `alice-ingest` token, 2-3 file batches), which surfaced three real bugs the
PR's own mocked/in-process test suite (`services/ingestion-api/tests/test_upload_batch.py`)
did not catch:

1. Every file but the last in a batch response serialized with `"document": {}`. Pydantic
   passes an already-typed model instance through by reference rather than copying it, so
   `BatchUploadItem.document` held the live SQLAlchemy-tracked `Document` -- each
   subsequent file's `session.commit()` (`expire_on_commit`, the default) wiped the
   earlier ones' loaded attributes. Existing tests accessed `item.document` as a plain
   Python attribute, which transparently re-fetches through the still-open session and
   never exercised the actual JSON-serialization path (`jsonable_encoder`) that FastAPI
   uses for the real response. Fixed with `document.model_copy()`; the regression test
   added for this asserts on `jsonable_encoder(results)` specifically so it can't recur
   unnoticed. This is also *why* the reported UI symptom existed: the frontend's per-row
   poll only starts when `document.id` is present.
2. The upload page's batch refactor deleted the blanket form `input`/`change` listener
   that drove the readiness checklist, without a replacement -- classification, access
   scope, and source-originator/doc-type stopped updating live. Restored.
3. Only `HTTPException` (empty file, unsupported type) was isolated per-file in the batch
   loop. An object-store or DB failure partway through one file propagated uncaught,
   returning a bare 500 that silently dropped every already-committed, already-queued
   file's result from the response -- reproduced with a flaky object-store stub that fails
   on the second of three files. Now isolated the same way validation failures already
   were.

## What's stubbed vs working

**Status-label convention (P1, REQUIREMENTS.md Section 11):** a bare "works" claim conflates
three genuinely different confidence levels, so this doc (and `README.md`/`ARCHITECTURE.md`)
distinguish them explicitly wherever it matters:
- **Implemented** — the code exists and does what it says, but hasn't been executed at all
  in this environment (e.g. no network access to a dependency, or a code path only a live
  cluster would exercise).
- **Tested against mocks/in-process substitutes** — actually run, but against a stand-in for
  a real dependency: an in-memory SQLite DB instead of Postgres, a mocked Qdrant/NATS/object-
  store client, a hand-crafted JWT instead of a real Keycloak token, a real `TestClient`/
  `uvicorn` round trip instead of a live `docker compose up`. Confirms the logic is correct
  in isolation; does not confirm the real dependency's actual behavior (auth quirks, network
  failure modes, version-specific API behavior) matches the stand-in.
- **Validated against a live environment** — actually run against the real thing (a live
  Postgres/Qdrant/NATS/Keycloak, a real `docker compose up`, a real MCP client SDK). This is
  the only level that rules out surprises the mock/substitute couldn't reproduce.

Every bullet below and every "Not tested against..."/"Smoke-tested..." caveat elsewhere in
this repo's docs is written to make clear which of these three levels it's claiming.
NFR-11/NFR-12 and NFR-13's happy path have since moved from "tested against mocks" to
"validated against a live environment" (a real `docker compose up` run); NFR-13's
failure-injection revert logic specifically, and the P1 batch (the `ALL_AUTHENTICATED`
rename, the prompt-injection mitigation), remain "tested against mocks" only. Treat the
absence of an explicit level as a bug in
the docs, not a silent "it works" — flag it if you find one.

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
- **Curation "List" dashboard (issue #266)** — a second curator-facing page,
  `/curate/list`, alongside the pending-review queue at `/curate` (a "Queue"/"List"
  sub-nav switches between them). Lists every document across the curator's
  `rag-curate:<org>` orgs regardless of status (`GET /curate/documents`, filterable by
  status/classification and a case-insensitive filename/originator/type/uploader search),
  and lets a curator correct a document's metadata *after* it has already cleared
  curation (`PATCH /curate/documents/{id}`) without going through supersession —
  Classification/Releasability/Access-scope edits are re-checked against the curator's
  own authority exactly like an approval correction, and are propagated to Qdrant's
  payload copy with the same NFR-13 revert-on-commit-failure behavior as approve/reject.
  Deletion on this page calls the existing, separately-gated `rag-purge` endpoint
  (`DELETE /documents/{id}`, see NFR-12/purge below) rather than a new one — the delete
  action is hidden client-side for a curator who doesn't hold that role, since they'd
  just get a 403. The queue page's feedback is human-readable now too (an outcome
  sentence, not a raw JSON dump), and its issue #138 advisory box uses the portal's theme
  tokens instead of a hardcoded light-mode color. **Tested against mocks only** (unit
  tests covering scoping/filters/authority/NFR-13-revert against an in-memory SQLite
  session) — not yet exercised against a live Postgres/Qdrant pair or a browser.
- **Curators bound by access_scope (issue #277, gap G1)** — a same-org, cleared,
  releasability-holding curator whose groups/org/sub don't match a *pending* document's
  `access_scope` no longer sees it in the queue (`GET /curate/queue`/`/curate/documents`)
  and can't approve or reject it directly by id either (`_check_curator_authority`) — a
  hard requirement, the same as clearance and releasability, with no fallback and no
  grace period. If no curator in an org happens to hold the right group/org/sub for a
  document's `access_scope`, that document has no one who can review it until an admin
  fixes the provisioning (adds the right group to a curator, or corrects the tag) — a
  deliberate choice, not a gap: an automatic fallback was considered and rejected as
  defeating the purpose of need-to-know. **Tested against mocks only** (unit tests
  against the shared `access_scope_authorized` predicate, plus service-level tests
  covering queue visibility and the approve/reject hard block against an in-memory
  SQLite session) — not yet exercised against a live Postgres pair or a browser.
- **Curator content view + hidden-instruction content advisory (issue #284)** — before
  this, no route anywhere in `ingestion-api` served a curator the actual text of a
  document they were being asked to approve; the curation gate reviewed metadata
  (filename, tags, the #138 marking advisory), never content. `GET /curate/{id}/content`
  now returns a `pending_review` document's parsed chunk text — read back from the vector
  store (`VectorStore.fetch_document_chunks`), i.e. exactly what retrieval will serve if
  approved, not a re-render of the original upload — gated by the same
  `_check_curator_authority` order approve()/reject() already use (org 404, then
  clearance/releasability/`access_scope` 403), 409 once the document has left
  `pending_review`. `/curate`'s queue page gets a "View content" button per row
  (collapsed, fetched on first open) rendered the same textContent-only way as every
  other field on that page (issue #207). Alongside it, `common/content_advisory.py`
  scans a document's own parsed text at ingestion time for invisible/control Unicode
  characters (the Cf-category "ASCII smuggling" trick, among others) and a short list of
  common prompt-injection trigger phrases, folded into the same `document.tagging_advisory`
  JSON column and advisory box the #138 marking-mismatch/#307 precedent/#308 LLM-suggestion
  findings already share — advisory only, never blocking, same fail-safe posture as the
  rest of that family. **Validated against a live environment** (2026-08-01, real
  `docker compose up` stack): uploaded a document containing an injection-shaped sentence
  as `alice-ingest`, confirmed the worker flagged it end-to-end (`tagging_advisory.
  content_advisory.findings`, audit entry) and that `GET /curate/{id}/content` returned
  its real parsed text as `carol-curator`; confirmed 403 for a `rag-query`-only identity,
  404 for a bogus id, and 409 once a document left `pending_review`; and drove the actual
  `/curate` page with a real Chromium instance (Playwright) logged in as `carol-curator`,
  clicking "View content" and confirming the chunk text and content-advisory hint render
  with no console errors. That live browser run also surfaced and fixed an unrelated,
  pre-existing bug in `curate.html`: its inline `<script>` lived inside `{% block content
  %}`, which renders (and, via its own unconditional `loadQueue()` call, *executes*)
  before `base.html`'s later `<script>` defines `authHeaders()` — a plain `ReferenceError`
  on every page load that silently prevented the queue from ever populating. Moved into
  `{% block scripts %}`, the pattern `admin.html`/`login.html`/`upload.html` already use
  correctly. The identical bug still affects `curate_list.html` and `notifications.html`
  (issue #323, filed rather than fixed here — out of scope for this change).
- **Sensitive-data-pattern curator advisory (issue #342, Phase 1)** — the ingestion
  worker's parse stage now also scans a document's own parsed text with
  `common/pii_scan.py`, a dependency-free regex pass for US SSN, Luhn-valid credit card
  numbers, checksum-valid bank routing numbers, well-known API-key/token prefixes
  (AWS/GitHub/Slack), a generic `key/token/secret = "..."`-shaped assignment, and PEM
  private-key block headers — folded into the same `document.tagging_advisory` JSON
  column and `/curate` advisory box the rest of this family (#138/#284/#307/#308) shares,
  under a `pii_advisory` key, same fail-safe/never-blocks posture. Findings never echo
  the matched sensitive value: `detail` is a fixed label naming the kind of pattern, and
  the context excerpt has the matched span itself replaced with `[REDACTED]`. Phone
  numbers, email addresses, and passport/driver's-license numbers are deliberately out of
  scope for this phase — see the module docstring. `docs/governance.md`'s Non-goals
  section is amended alongside this change to distinguish this narrow, flag-only signal
  from the PII redaction/data-subject-rights tooling that remains explicitly out of
  scope. The issue's proposed second layer — an LLM-assisted pass for context-dependent
  PII a regex can't catch — is deliberately deferred to a follow-on issue, not built here.
  **Tested against mocks only** (`tests/unit/common/test_pii_scan.py` for the pure
  detection logic, including that no finding ever echoes the matched value;
  `services/ingestion-worker/tests/test_pii_advisory_processing.py` for the worker glue
  against an in-memory SQLite session) — not yet exercised against a live
  `docker compose up` stack or a browser.
- **LLM-assisted PII/sensitive-info advisory (issue #343, Phase 2 of #342)** — the
  deferred second layer #342 itself proposed: an opt-in pass (`PII_LLM_MODEL`, empty
  by default) that asks a text-generation model on the stack's Ollama to look for
  context-dependent sensitive personal/financial information the regex pass
  structurally can't catch (a spelled-out or differently-formatted SSN, a non-US
  national ID number, freeform personal/financial detail embedded in prose). Folded
  into the same `document.tagging_advisory` JSON column and `/curate` advisory box
  as a sibling of Phase 1's `pii_advisory.findings` — `pii_advisory.llm_findings` —
  not a fourth advisory surface. Same fail-safe posture as every other suggester in
  this family: any error, including an unreachable/misconfigured model, is swallowed
  and logged (`nexus_rag_ingestion_worker_pii_llm_findings_total{outcome=...}`), never
  blocking or delaying ingestion. The prompt asks the model not to repeat the
  sensitive value itself in its answer, but that is a prompt instruction the model
  could ignore, not a code-enforced guarantee the way Phase 1's redacted `context`
  field is — `kind`/`rationale` must be treated as exactly as attacker/model-controlled
  as the LLM classification suggestion's `rationale` (textContent only, never
  innerHTML, in `curate.html`). Unit-level behavior is **tested against mocks**
  (`services/ingestion-worker/tests/test_pii_llm_advisory.py`, respx-mocked Ollama;
  `test_pii_llm_advisory_processing.py`, worker glue against an in-memory SQLite
  session); the enabled path is **validated against a live environment**: real
  `docker compose up` (`PII_LLM_MODEL=qwen2.5:3b-instruct`, the model already pulled
  for `GENERATION_MODEL`/`CLASSIFICATION_MODEL`), a document worded with a spelled-out
  Social Security number in prose (no dashed `###-##-####` form, so Phase 1's regex
  scan stayed clean) uploaded through the real `POST /documents` API tagged
  `CUI`/`memo`, confirmed end to end: the worker's real Ollama call returned
  `{"kind": "spelled-out SSN", "rationale": "Social Security number provided in a
  written description rather than in a labeled field"}` — describing the finding
  without repeating the actual digits — `GET /curate/queue` surfaced it under
  `pii_advisory.llm_findings` alongside Phase 1's empty `findings` in the same
  advisory object, `nexus_rag_ingestion_worker_pii_llm_findings_total{outcome=
  "findings"}` incremented on `/metrics`, and approving the document preserved the
  finding in the persisted `tagging_advisory`. (The seeded sample corpus itself
  triggered 3 genuine findings / 5 clean runs on the same metric, purely as a side
  effect of `PII_LLM_MODEL` being enabled during `seed-sample-data` — not a claim
  about those documents' real content, just confirmation the pass runs unattended
  across a realistic batch without error.)
- **LLM-assisted verification of Phase 1's own regex findings (issue #378)** — #342's
  checksum-validated patterns (Luhn for credit cards, ABA for bank routing) keep any
  *individual* random digit run's false-positive chance low, but a numeric-heavy
  technical document (a manual full of part numbers, page/section references,
  revision codes) offers many candidate digit runs, so at the document level a false
  flag turns out to be common rather than rare — reported in practice against real
  manuals. Rather than raise the regex pass's bar (which would just start missing
  genuine matches), when `PII_LLM_MODEL` is enabled the same model is also asked to
  judge each Phase 1 finding using only its already-redacted `context` excerpt (the
  matched value itself was never in that excerpt, so there is nothing further to send
  the model beyond what the curator already sees). Every finding is annotated in place
  with an `llm_verdict` (`likely_false_positive` + a short `rationale`) — this never
  filters, drops, or dims a finding; the curator still sees and decides on every regex
  match, same "advisory only, never trusted to redact/decide/gate" posture as
  everything else in this family. Own metric,
  `nexus_rag_ingestion_worker_pii_llm_verification_total{outcome=...}`, kept separate
  from `pii_llm_findings_total` so an `unavailable` outcome is unambiguous about which
  half of the `PII_LLM_MODEL` call budget failed. Unit-level behavior is **tested
  against mocks** (`services/ingestion-worker/tests/test_pii_llm_advisory.py`'s
  `verify_pii_findings` cases, respx-mocked Ollama; `test_pii_llm_advisory_processing.py`'s
  verification cases, worker glue against an in-memory SQLite session); the enabled path
  is **validated against a live environment**: real `docker compose up`
  (`PII_LLM_MODEL=qwen2.5:3b-instruct`), a document worded as a field-service manual
  (part numbers/document-control numbers chosen so two are Luhn-valid and one is
  ABA-checksum-valid, e.g. `4111 1111 1111 1111` labeled "Part No. ... Rev 2") uploaded
  through the real `POST /documents` API tagged `CUI`/`manual`, confirmed end to end:
  Phase 1's regex scan flagged all three as expected (2 `credit_card`, 1 `bank_routing`
  finding), the worker's real Ollama verification call annotated each with
  `llm_verdict.likely_false_positive: true` and an accurate rationale (e.g. "Part number
  and revision reference match credit card number pattern", "Document control number
  matches bank routing number pattern") — exactly the false-positive pattern #378
  reported — and `nexus_rag_ingestion_worker_pii_llm_verification_total{outcome=
  "verified"}` incremented on `/metrics`. `GET /documents/<id>` confirmed every original
  finding stayed present and unfiltered alongside its verdict. (The same run also showed
  `pii_llm_findings_total{outcome="unavailable"}` incrementing a few times under heavy
  concurrent Ollama load from an unrelated backlog of large PDFs also queued in this dev
  stack — the pre-existing degrade-to-`None`-on-timeout contract working as designed,
  not a regression.) `scripts/calibrate_tagging_advisory.py` did not yet incorporate
  `llm_verdict` at the time -- closed by issue #380 below.
- **Calibration for #378's `llm_verdict` (issue #380)** — `scripts/calibrate_tagging_advisory.py`
  now scores how well the verdict tracks curation practice, not just whether a curator
  acted on a PII finding at all: a new `pii_regex_llm_verdict` (`PiiVerdictTally`) checks,
  for documents where #378's LLM verification ran and every finding in the document landed
  on the *same* verdict, whether the curator's decision agreed (a `likely_false_positive`
  verdict agrees with approving unchanged; a not-`likely_false_positive` verdict agrees
  with rejecting or correcting). Unlike `pii_regex`/`pii_llm`'s `acted_on_rate`, this one
  has an actual prediction to score against, so it reports `agreement_rate` like the
  classification-tag suggesters do, and (opt-in, same as them) participates in
  `--min-agreement`. A document where the verdicts disagreed with each other, or only
  some of its findings got verified, is counted in `skipped` rather than guessed at --
  there's no single per-document verdict to score a per-document curator decision
  against in that case. New fields on the audit-entry outcome
  (`services/ingestion-api/app/routes/curate.py`'s `_tagging_advisory_outcome`):
  `pii_regex_llm_verified_count`/`pii_regex_llm_likely_false_positive_count`, counts
  only, omitted entirely (not zeroed) when verification never ran for that document.
  Unit-level behavior is **tested against mocks**
  (`tests/unit/test_calibrate_tagging_advisory.py`'s `TestPiiVerdictTally`/
  `TestAggregatePiiVerdict`, pure aggregation logic against constructed audit rows;
  `services/ingestion-api/tests/test_tagging_advisory_linkage.py`'s new cases, linkage
  against an in-memory SQLite session); the DB fetch itself is **validated against a
  live environment**: real `docker compose up` with `PII_LLM_MODEL` re-enabled, a fresh
  upload of the same field-service-manual fixture #378 used, approved by `carol-curator`
  through the real `POST /curate/<id>/approve` API, confirmed end to end via
  `docker compose --profile calibration run --rm calibrate-tagging-advisory` against
  the real dev-stack Postgres: the audit row carried the new
  `pii_regex_llm_verified_count`/`pii_regex_llm_likely_false_positive_count` fields, and
  the printed report picked them up. This run happened to land 2-of-3 findings verdicted
  `likely_false_positive` rather than 3-of-3 (real model output varies run to run) --
  which live-exercised the mixed-verdict `skipped` path rather than a scored one
  (`pii_regex_llm_verdict: ... skipped=1`), itself a useful confirmation that documents
  without a single clean verdict are excluded rather than guessed at. The
  all-agreed/all-overridden scoring paths are covered by the mocked unit tests above,
  same "logic is unit-tested, the live run confirmed wiring + wrote the right JSON
  shape" split as elsewhere in this doc (see "Deliberately not a pass/fail CI gate" in
  `docs/testing.md`).
- **Precedent-tag advisory over the approved corpus (issue #307, Phase 2 of #138)** —
  the ingestion worker runs a dense-only kNN lookup (`VectorStore.find_similar_approved`)
  against every `approved` chunk, using the centroid of the document's own chunk
  embeddings it already computed for storage (no extra model call). The classification/
  releasability of the nearest matches are folded into the same `document.tagging_advisory`
  JSON column and `/curate` advisory box Phase 1's marking-mismatch finding uses, only
  surfaced when the nearest precedent disagrees with the assigned classification —
  e.g. "4/5 nearest approved documents are tagged SECRET // REL TO NATO." Same posture
  as Phase 1: advisory only (never mutates a tag), fail-safe (any error, including an
  unreachable Qdrant, is swallowed and logged, leaving ingestion unaffected), and scoped
  to `approved` chunks only, never `pending_review`/`rejected` (FR-26). **Tested against
  mocks only** (worker-side unit tests against a fake vector-store client, plus
  `qdrant_backend.QdrantStore.find_similar_approved` tests against a fake Qdrant client
  mirroring `hybrid_query`'s existing fan-out tests) — not yet exercised against a live
  Qdrant or a browser. `MilvusStore.find_similar_approved` is implemented but has no
  dedicated unit test yet, matching the existing gap on `MilvusStore.hybrid_query`.
- **LLM classification suggestion (issue #308, Phase 3 of #138)** — the ingestion
  worker asks a text-generation model on the stack's Ollama (`CLASSIFICATION_MODEL`,
  opt-in, empty by default) to zero-shot classify a document against the configured
  `ClassificationLevel` list plus a free-text doc_type/program_community guess, with a
  confidence score and rationale. Folded into the same `document.tagging_advisory`
  column/advisory box as Phase 1/2, only surfaced on disagreement (asymmetric: only an
  under-classification is flagged for Classification, same as Phase 1/2; any doc_type
  difference is flagged regardless of direction). Advisory only (never mutates a tag),
  fail-safe (any error, including an unreachable/misconfigured model, is swallowed and
  logged), and a suggested Classification value outside the configured list is dropped
  rather than invented. See "LLM classification suggestion (optional, #308)" above for
  the full write-up, including the live-validation run (real Ollama call, real
  `/curate/queue` and audit-entry confirmation) -- unit-level behavior is tested against
  mocks (respx-mocked Ollama client tests, worker-glue tests against an in-memory SQLite
  session, curator-decision audit-linkage tests); the enabled path is **validated
  against a live environment**.
- **Adaptive calibration loop over curator corrections (issue #309, Phase 4 of #138)** —
  `scripts/calibrate_tagging_advisory.py` mines every `document.approve`/`document.reject`
  audit entry's `tagging_advisory` outcome (already written by `curate.py` for #306/#307/
  #308) and reports, per suggester (Phase 1 marking-mismatch, Phase 2 precedent, Phase 3
  LLM classification, plus Phase 1's releasability-caveat flag), how often the curator's
  final decision agreed with what was flagged vs. overrode it — the "measure suggester
  accuracy over time" half of #309 (FR-30/FR-32 posture). Run on demand or on a schedule
  with `docker compose --profile calibration run --rm calibrate-tagging-advisory`; pass
  `--history-dir` to persist each run and print an informational trend line against the
  prior one. It is reporting only by default (no CI gate — a curator override is not, by
  itself, proof a suggester was wrong); `--min-agreement` is an opt-in floor for a
  deployment that wants one. Connects to Postgres as a new, dedicated,
  **SELECT-only-on-audit_log** role (`nexus_rag_audit_reporting`,
  `infra/postgres/ensure-roles.sh`/`apply-service-grants.sh`) rather than through any of
  the four services — NFR-2 keeps every application role's own credentials INSERT-only on
  `audit_log`, so reading the curation trail has to be a distinct, attributable identity.
  The issue's other clause, "refresh Phase 2's precedent index from the corrected/approved
  set," needed no code: `find_similar_approved` already queries `status == "approved"`
  live against Qdrant on every ingestion, so there is no index to refresh — it already
  reflects the current corrected/approved corpus. **Tested against mocks only** (the pure
  aggregation logic — `tests/unit/test_calibrate_tagging_advisory.py` — against
  constructed audit rows, plus extended curator-decision audit-linkage tests for the two
  new outcome fields `assigned_classification`/`marking_mismatch_flagged` this needed) —
  the DB fetch itself, the new `nexus_rag_audit_reporting` role/grants, and the
  `calibration` compose profile have not been exercised against a live Postgres.
- **PII-advisory calibration coverage (issue #345)** — the adaptive calibration loop
  above didn't cover the sensitive-data-pattern advisory family (#342's regex pass,
  #343's LLM-assisted pass): `curate.py`'s `_tagging_advisory_outcome` now also embeds
  finding kinds/counts for `pii_advisory.findings` and `pii_advisory.llm_findings` into
  the approve/reject audit entry, and `calibrate_tagging_advisory.py` reports them as
  `pii_regex`/`pii_llm`. Unlike the classification-tag suggesters, a PII finding carries
  no classification/releasability target to rank-compare against, so this uses a
  different, purpose-built `PiiTally.acted_on_rate`: rejecting the document or approving
  it with a changed classification counts as "acted on," approving it unchanged does
  not — see the script's module docstring for the reasoning. Unit-level behavior is
  **tested against mocks** (extended `tests/unit/test_calibrate_tagging_advisory.py` and
  `services/ingestion-api/tests/test_tagging_advisory_linkage.py`); the full path is
  **validated against a live environment**: real `docker compose up`, four documents
  uploaded through the real `POST /documents` API each carrying a dashed SSN (one also
  worded to trip the LLM-assisted pass), curated through the real `/curate/.../approve`
  and `/curate/.../reject` APIs across all three outcomes (approved unchanged, approved
  with a corrected classification, rejected) — confirmed the audit role's
  INSERT-only-on-`audit_log` grant actually rejects a read with the application role
  (`psycopg.errors.InsufficientPrivilege` querying as `nexus_rag_ingestion_api`), the
  `pii_regex_kinds`/`pii_llm_kinds`/`*_count` fields round-trip through the real
  Postgres JSON column, and `docker compose --profile calibration run --rm
  calibrate-tagging-advisory` (rebuilt image) reported `pii_regex`/`pii_llm` with
  `flagged=4 approved_unchanged=2 approved_corrected=1 rejected=1 acted_on_rate=50.00%`
  — the exact tally the four decisions above should produce — including a correct
  `--history-dir` trend line on a second run.
- **Reconnaissance-shaped query detection (issue #426, #127 gap #4)** —
  `scripts/detect_query_anomalies.py` mines `query`/`query.denied` audit rows over a
  lookback window (default 60 minutes) for four per-identity signals: raw attempt-rate
  (`high_volume`), a sustained personal denial rate distinct from the global
  `NexusRagQueryDeniedSpike` volume alert (`high_denial_ratio`), a high share of
  successful queries resolving to 0-1 chunks (`narrow_probe_shaped` — the substitute
  for near-duplicate-query-text detection, since #125 means no query text exists to
  diff), and repeated denial-then-success sequences within a short window
  (`boundary_mapping`). Run on demand or on a schedule with `docker compose --profile
  anomaly-detection run --rm detect-query-anomalies`; reporting only by default, same
  posture as the calibration script. Reuses the same `nexus_rag_audit_reporting` role
  (no new grant) and, unlike that script, also pushes a content-free, per-signal
  *count* of flagged identities plus a staleness timestamp to Prometheus via
  Pushgateway — deliberately never a per-identity label, for the same reason
  `orchestration-mcp/app/metrics.py` gives for never labeling a metric by user. Two new
  alert rules, `NexusRagQueryAnomalyDetected`/`NexusRagQueryAnomalyDetectionStale`, ship
  in `infra/observability/prometheus/rules/nexus-rag.yml`.

  **Validated against a live environment.** A real `docker compose up`, real
  `bob-query`/`alice-ingest` Keycloak tokens, and 32 top_k=1 `/debug/rag_search` calls as
  `bob-query` plus 11 as `alice-ingest` (who holds no `rag-query` role) produced real
  `audit_log` rows; `docker compose --profile anomaly-detection run --rm
  detect-query-anomalies` correctly flagged `bob-query` for `high_volume` +
  `narrow_probe_shaped` and `alice-ingest` for `high_denial_ratio`, with
  `boundary_mapping=0` for both. With `--profile observability`'s `pushgateway`/
  `prometheus` also up, a second run's metrics landed in Pushgateway (confirmed
  content-free — no `actor_sub`/username in the payload), Prometheus scraped them, and
  `NexusRagQueryAnomalyDetected` fired for the three expected signal labels and stayed
  silent for `boundary_mapping`. Also confirmed the fail-open path: running the job
  before Pushgateway was up printed a `WARNING` and still exited 0.

  This live run also caught a real inaccuracy in the original design write-up, corrected
  before merge: `boundary_mapping` was described (here, in `detect_query_anomalies.py`'s
  docstring, and in `docs/threat-model.md`) as "filter-boundary mapping" — probing where
  an FR-26 classification/releasability/access-scope filter's edge sits. Sending
  `alice-ingest` 11 queries produced 11 `query.denied` rows and zero `query` rows,
  which is the tell: `rag_search.py`'s only `query.denied` path is the coarse missing-
  `rag-query`-role gate (`if not claims.can_query`), not a per-query FR-26 mismatch — an
  out-of-scope query returns a *successful* empty result, never a denial. So the signal
  as built can only fire when an identity's `rag-query` grant changes state mid-window
  and is used immediately after (a role revoked then reinstated, or a delayed token
  refresh picking up a just-granted role) — still worth a look, just narrower than the
  original "probing an access boundary" framing claimed. `docs/threat-model.md`
  section 4 and `detect_query_anomalies.py`'s docstring both carry the corrected
  description.
- **Uploader notifications on curator decisions (FR-15)** — approving or rejecting a
  document writes an in-app `Notification` row for the uploader
  (`common/models.py`/`app/routes/notifications.py`), with the rejection reason
  included for rejections. No SMTP/email infra in this dev stack, so this is a
  discrete, markable-as-read record (`GET /notifications/list`, `POST
  /notifications/{id}/read`, both scoped to the recipient) rather than an email/push
  notification — a real notification the uploader doesn't have to already know a
  document ID to find, not just data that happens to be visible if you go looking.
  **Fixed and validated against a live environment** (2026-08-01): the JSON list
  endpoint used to be registered at the same path (`GET /notifications`) as the page
  route in `app/main.py`, and Starlette's first-match routing meant the page was
  permanently unreachable — every request, browser navigation included, hit the JSON
  API's hard 401 instead (issue #328; present since the feature's original PR, #10, not
  a regression from #323's template fix). Moved the JSON endpoint to `GET
  /notifications/list`, mirroring the `/curate/queue`+`/curate/documents` vs.
  `/curate`+`/curate/list` split. Confirmed live (`docker compose up`): unauthenticated
  `GET /notifications` now returns the anonymous-landing page (200 HTML, matching `/`,
  `/curate`, `/admin`, `/search`) instead of a 401, and, authenticated as
  `alice-ingest`, the page renders and its own script successfully pulls real
  notification data from `GET /notifications/list`.
- **Document parsing, chunking, embedding, and Qdrant storage (FR-3..FR-6)** —
  `services/ingestion-worker/app/{parsing,chunking,embedding}.py`, run by the
  `ingestion-worker` service, not `ingestion-api` (see NFR-11 below for why).
  Handles PDF, DOCX, PPTX, XLSX, TXT/MD, HTML; chunks respect section/heading/page/slide
  boundaries (~512 words, ~15% overlap — word-based, not a model-specific tokenizer;
  both are env-configurable per FR-4 via `CHUNK_TARGET_WORDS`/`CHUNK_OVERLAP_RATIO` --
  `.env.example`/`docker-compose.yml` here, `ingestionWorker.chunkTargetWords`/
  `chunkOverlapRatio` in the Helm chart).
  A curator's approve/reject (and any tag corrections made while approving) propagate
  to the chunks' Qdrant payload, not just the Postgres row (`common/qdrant_store.py`,
  called from `ingestion-api/app/routes/curate.py`) — that's what actually changes
  query-time visibility.
- **One Qdrant collection per Classification level, not one shared collection (issue
  #229) — tested against mocks, not yet validated against a live environment.**
  `common/qdrant_store.py` derives a collection per admin-configured Classification value
  and every ingestion/curation/supersession/purge path is scoped to it; `qdrant_backend.py`
  fans `hybrid_query` out over every collection the caller is cleared for and fuses results
  by rank rather than by score (`common/vector_store.fuse_ranked`), since BM25 IDF is now
  computed per collection, each now classification-skewed rather than corpus-wide. Pure-logic
  coverage exists
  (`tests/unit/common/test_classification_collections.py`,
  `test_qdrant_backend_fanout.py`, `test_rrf_fusion.py`) against a fake Qdrant client, but
  this has not yet been run against a real `docker compose up` stack — that would confirm
  actual collection lifecycle behavior and whether recall holds under the new IDF scope.
  `scripts/golden_queries.json` has not been re-baselined against it either; see
  `docs/testing.md`'s #229 section for what specifically remains. The Milvus backend
  (#160) explicitly does not implement this split (`common/milvus_store.py`'s module
  docstring) and keeps its pre-#229 single-collection behavior.
- **Async ingestion pipeline with real progress states (FR-8), on a durable queue
  (NFR-11)** — `POST /documents` (`ingestion-api`) validates the request synchronously
  (auth, mandatory tagging, FR-7 supersede-target checks), durably stores the original
  file (`common/object_store.py`), and returns `202 Accepted` with `status: queued`
  immediately. It then publishes the document ID to NATS JetStream
  (`common/job_queue.py`) rather than running the pipeline itself; `ingestion-worker`
  is the durable consumer that actually does it
  (`services/ingestion-worker/app/processing.py:process_document`), moving the row
  through `queued → processing → embedded → pending_review`, or to `failed` with a
  message in `processing_error` if parsing or embedding errors out (NFR-7: caught, not
  left to crash the worker) — corrupt, password-protected, empty, unsupported, or
  zip-bomb-shaped (`app/parsing.py`'s `_check_zip_bomb`: a `.docx`/`.pptx`/`.xlsx` whose
  ZIP entries would decompress past 200MB or at a >200:1 ratio is rejected before
  python-docx/python-pptx/openpyxl ever touch it, since `MAX_UPLOAD_BYTES` only bounds
  the *compressed* upload) files land here instead of a synchronous 4xx like before this
  change. `MAX_UPLOAD_BYTES` itself is env-configurable (FR-9's "configurable size
  limit"), default 50MB -- see `.env.example`/`docker-compose.yml` here,
  `ingestionApi.maxUploadBytes` in the Helm chart. `POST /documents/batch` (FR-34/#356)
  shares the same per-file `MAX_UPLOAD_BYTES` limit and adds its own `MAX_BATCH_FILES`
  file-count cap (default 25, `ingestionApi.maxBatchFiles` in the chart) -- see the
  tmpfs/ingress note above for the aggregate-body-size interaction between the two.
  `GET /documents/{id}` (scoped to the
  uploader) polls current status; the ingestion UI polls it automatically after upload.
  A crash or restart of `ingestion-worker` mid-processing does not strand the document:
  `process_document` only acks the JetStream message on a terminal outcome (success or
  a permanent parse/embed failure); an unexpected/transient error (Qdrant or the DB
  unreachable, etc.) is left un-acked, so JetStream redelivers it to another attempt
  after `ACK_WAIT_SECONDS`. This is what replaced the earlier `BackgroundTasks`-based
  pipeline, which had no equivalent recovery and left a document stuck in `processing`
  forever if the process restarted mid-document.
- **Hybrid dense+BM25 retrieval and reranking (FR-24/FR-25)** —
  `services/orchestration-mcp/app/rag_search.py` queries a dense semantic leg and a BM25
  sparse leg (`common/sparse_embedding.py`, Qdrant's own `fastembed`/`Qdrant/bm25` model)
  in parallel via Qdrant's native `Prefetch`/`FusionQuery` (Reciprocal Rank Fusion), with
  the access filter applied to *both* legs so neither can be used to bypass FR-26. The
  fused candidates are then reranked by `reranker-service` (`app/reranking.py`), with a
  graceful fallback to the fused order (noted in the response, not hidden) if that
  service is unreachable. After ranking (either path), same-document chunks whose
  indices are adjacent are collapsed to the better-ranked one and the freed slot is
  backfilled from the rest of the candidate pool, since FR-4's chunk overlap means
  neighbouring chunks share text by construction (#395); the response note reports
  how many were collapsed. An optional relevance floor (`RERANK_SCORE_FLOOR`, issue
  #394, unset/off by default) then drops any candidate whose post-boost cross-encoder
  score falls short; a query where every candidate drops returns `[]` with an explicit
  note instead of its least-bad `top_k`, routed as `queries_total{outcome="empty"}`
  with the reason in the FR-31 audit row, not folded into an ordinary success. **Validated
  against a live environment** (2026-08-05): on the 4-document dev corpus with the
  windowed reranker, answerable queries' best chunks scored -2.5..+8.9 and unanswerable
  ones -11.3..-2.8, so -5.0 is documented (`.env.example`, `values.yaml`) as a measured,
  permissive starting point rather than the shipped default. Those numbers are raw
  cross-encoder logits from this chart's own `reranker-service`; a #419 external
  `"tei"`/`"cohere"` endpoint typically returns a normalized 0..1 score instead, so the
  floor needs re-tuning per serving model. Not validated: `abstention_accuracy` moving
  for this reason through the full LibreChat generation path (#383's harness needs the
  manually created agent).
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
  and `/debug/rag_search` via MCPServer's own `custom_route` instead of wrapping the app in
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
  (never the token itself in browser-reachable storage). Issue #213: the stored
  access/refresh/id tokens are encrypted at rest (`common/token_crypto.py`, keyed by
  `SESSION_TOKEN_ENCRYPTION_KEY`) rather than dropped or hashed — both alternatives were
  considered and rejected because `refresh_token` drives real silent renewal and
  `access_token` is forwarded verbatim to downstream calls, so the app has to be able to
  read them back in plaintext; encryption is the only option that doesn't regress that.
  Confirmed live against a real Postgres: a raw SQL read of `user_sessions` returns
  ciphertext, the ORM round-trip returns the original token. This is the project's chosen
  position, not an open gap. `app/deps.get_current_user`
  resolves that cookie to the same `UserClaims` as the header-based bearer-token path used
  by curl/API/MCP callers — transparently refreshing an expired access token via the
  stored refresh token — so no enforcement logic forks between the two. "Log out" performs
  a real Keycloak RP-initiated logout (`id_token_hint` + `post_logout_redirect_uri`), not
  just a local session clear, so logging back in re-prompts for credentials — **confirmed
  live against a real Keycloak** (issue #254 fix): login, log out, log back in and Keycloak
  re-prompts for credentials rather than silently re-authenticating the same user. (The
  original implementation had `/auth/logout` answer with a 303 and let the nav's `fetch()`
  follow that redirect itself; that hop is not a top-level browser navigation, so
  Keycloak's own `SameSite=Lax` SSO cookie never rode along and the SSO session outlived
  every logout. The fix has `/auth/logout` hand back the Keycloak logout URL as JSON and
  has the client navigate there via `window.location` instead — a real top-level
  navigation.) The nav bar's logged-in-username display
  (`get_current_user_optional`, used by the three page routes) remains
  sandbox-`TestClient`-verified only. See "Stubbed / TODO" below for what's still
  Compose-only.
- **The whole ingestion UI gated behind a login landing page, plus admin-configurable
  branding and a mandatory-acceptance login banner (issues #246/#248).** Every page route
  now renders `login.html` — centered logo, application name, login button, nothing else —
  for an anonymous visitor instead of the real page; the top nav (`base.html`'s `<header>`)
  doesn't render at all until signed in. Confirmed against a real `docker compose up`, not
  just sandbox `TestClient` calls: rebuilt the `ingestion-api` container, set branding
  (`app_name`, `logo_url`) and the login popup (title/text/button text) via `dave-admin`'s
  real bearer token against the live `/admin/branding` and `/admin/login-banner` endpoints,
  then confirmed with `curl` that the anonymous login page reflects them — custom tab
  title, favicon, header/footer branding on an authenticated page, the popup banner text,
  and the login button rendered `hidden` until an Accept click reveals it — plus
  `/login/declined`. Not yet run through a real browser: the actual Accept/Decline click and
  the post-login authenticated header render for a real Keycloak session cookie (as opposed
  to a hand-built `UserClaims` in a direct function call) are exercised by
  `tests/test_login_gate.py`/`tests/test_branding_login_banner.py` at the sandbox level, not
  against a live Keycloak session yet.
- **Nav gated per role, not just per authentication (issue #249).** The "Curation" nav
  link (`base.html`; renamed from "Curation queue" by issue #266, since it now covers
  both the `/curate` queue and `/curate/list` master-list pages) only renders for a user
  holding a `rag-curate:<org>` role,
  matching the existing `is_admin` gating on the Admin link — closing the gap between what
  the tab showed and what `/curate/*` already enforced (`require_curator`, `app/deps.py`),
  since a non-curator following the link only ever reached a 403. Notifications stays
  visible to every signed-in user rather than admin-gated as the issue originally proposed:
  notifications are uploader-scoped (`recipient_sub == doc.uploader_sub`, FR-15), and
  `rag-admin` grants no data access, so gating that tab on it would have hidden it from the
  users who actually receive one. Validated against a real `docker compose up`: rebuilt the
  `ingestion-api` container and confirmed in a real browser that `carol-curator` sees the
  Curation queue tab and `alice-ingest`/`bob-query`/`dave-admin` do not, with Notifications
  visible to all four. Sandbox-level coverage in `tests/test_login_gate.py`.
- **CSRF protection on cookie-authenticated routes (NFR-14)** — a double-submit cookie
  (`nexus_rag_csrf`, set alongside the session cookie at login, deliberately *not*
  `HttpOnly` so the page's own JS can read and echo it) checked against an `X-CSRF-Token`
  header (`app/deps.verify_csrf`) on every state-changing route: document submission,
  curation approve/reject, notification read, and the admin classification/releasability
  endpoints. Only enforced when a session cookie is present — a bearer-token caller (curl,
  MCP) is never CSRF-exposed and skips this check entirely, same reasoning as
  `get_current_user`'s two paths never forking enforcement logic. Sandbox-`TestClient`-
  verified (mismatched/missing header rejected, matching header passes, bearer-token
  callers unaffected, logout clears both cookies). **Issue #187: confirmed live against a
  real browser** (`scripts/verify_browser_csrf_logout.py`, wired into `e2e.yml`'s
  `browser-verify` job alongside `golden-query`) — the session cookie is actually
  `HttpOnly` and the CSRF cookie actually isn't, `base.html`'s JS can read exactly the
  cookie it's supposed to, missing/mismatched `X-CSRF-Token` is rejected and a matching
  one passes, and a full Keycloak RP-initiated logout (`id_token_hint` +
  `post_logout_redirect_uri`) actually ends the SSO session — a subsequent authenticated
  request 401s and logging back in re-prompts for real credentials rather than silently
  re-authenticating (issue #254's fix, now regression-tested rather than only manually
  confirmed once).
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
  The four first-party images (`ingestion-api`, `ingestion-worker`, `orchestration-mcp`,
  `reranker-service`) in `values.yaml` are pinned to `0.1.0` (matching `Chart.yaml`'s
  `appVersion`), and since the v0.1.0 tag those are real images on GHCR, not a placeholder
  — see `docs/releasing.md` for the release process that builds/pushes them. The Keycloak bump in
  particular (26.2 → 26.7.0) deserves a full `down -v` / `up` / realm-import / login retest
  before trusting it, given how many of the eight Keycloak bugs above turned out to be
  version-behavior surprises rather than code bugs.
- **Optional Milvus vector backend, either/or with Qdrant (issue #160)** —
  `VECTOR_BACKEND=qdrant` (the default, and the absence of the variable) is
  today's path byte-for-byte; `VECTOR_BACKEND=milvus` runs the same pipeline
  against Milvus Standalone (`docker compose --profile milvus up -d` plus
  `MILVUS_URL`/`MILVUS_TOKEN`; Helm: `vectorBackend` + `milvus.*`). One
  backend per deployment — never both. The FR-26 mandatory filter is built
  as a Milvus boolean expression with the exact clause-for-clause semantics
  of the Qdrant filter, applied to both hybrid legs, with string values
  escaped so a hostile claim value cannot widen the filter. The sparse leg
  deliberately reuses the same client-side fastembed BM25 vectors as Qdrant
  (not Milvus's server-side BM25 Function) so an A/B measures the engine,
  not the tokenizer. The collection uses Strong consistency because the
  curation flow relies on read-your-writes — surfaced by live validation,
  where bounded staleness made an approve flip invisible to the next query.
  Validated against a real Milvus v2.4.17 container: approved-only,
  classification ceiling, releasability holdings, cross-org isolation,
  curation status flip, provenance stamp, and chunk deletion all pass; not
  yet exercised by the golden-query e2e (the comparison harness run is the
  follow-up the issue defines). For local inspection (issue #163), Qdrant
  keeps its bundled dashboard while the Milvus profile adds the compatible
  Apache-2.0 Attu v2.4.12 UI; neither UI is exposed beyond host loopback.
- **Distributed tracing across the queue and the retrieval fan-out
  (issue #134)** — every service emits OpenTelemetry spans when
  `OTEL_EXPORTER_OTLP_ENDPOINT` points at an OTLP/HTTP collector
  (otel-collector, Tempo, ...); unset, tracing is a no-op. The deliberate
  piece is the queue boundary: `ingest.submit` (ingestion-api) and
  `ingest.process` (ingestion-worker, with parse/chunk/embed/qdrant.upsert
  children) form one trace because the W3C traceparent rides in the NATS
  *message headers* — the body stays a bare document id, so the #109
  malformed-payload guard and in-flight messages are untouched, and
  JetStream redelivery carries the same context (validated against a real
  NATS: nak → redelivery, headers byte-identical). Retrieval traces as
  `rag_search` → embed.query / qdrant.query / rerank, with the context
  propagating over httpx into reranker-service, whose request +
  `model.predict` spans nest under the caller's — the cross-encoder's time
  is no longer opaque. Span attributes are ids/counts/sizes only, never
  query or chunk text (#125's rule), and head sampling defaults to 5%
  (`OTEL_TRACES_SAMPLER_ARG`; ParentBased, so one decision covers a whole
  request tree). Helm: `observability.tracing.*`. Unit-tested with a real
  in-memory TracerProvider; not yet validated against a live Tempo.
- **SIEM export of audit events, and level-configurable structured logging
  (NFR-2, issue #73)** — every `audit_log` row (FR-31 funnels each ingestion,
  curation, retrieval, and purge event from every service through that one
  model) is forwarded as an RFC 5424 syslog message with a JSON payload the
  moment it is inserted, via a SQLAlchemy `after_insert` hook in
  `common/siem.py` — no per-call-site discipline required. Disabled unless
  `SIEM_SYSLOG_HOST` is set — any IP/hostname and port the environment's
  collector listens on (`SIEM_SYSLOG_PORT` default 514, or 6514 for tls;
  Helm: `observability.siem.*`). Three transports via
  `SIEM_SYSLOG_PROTOCOL`: `udp` (default), `tcp` (RFC 6587 octet-counted
  framing), and `tls` (RFC 5425 — the same framing inside a verified TLS
  session, for a collector on a protected segment: `SIEM_SYSLOG_CA_CERT`
  points at the CA that signed the collector's certificate, optional
  `SIEM_SYSLOG_CLIENT_CERT`/`SIEM_SYSLOG_CLIENT_KEY` for mutual TLS, and
  `SIEM_SYSLOG_TLS_VERIFY=false` exists as a loudly-logged debug-only
  escape hatch). To watch the export end to end locally, an opt-in
  stand-in collector prints every message it receives, tagged per
  transport: `docker compose --profile siem-debug up -d syslog-collector`,
  point services at it with `SIEM_SYSLOG_HOST=syslog-collector`, then
  `docker compose logs -f syslog-collector` (its TLS listener activates
  automatically once `infra/certs/generate-dev-certs.sh` has run).
  Fail-open on purpose: a collector
  outage logs one warning and never blocks the request path — the DB row
  remains the durable record either way. Denied actions (`query.denied`) go
  out at WARNING severity, everything else at NOTICE, facility 13 (log
  audit). Alongside it, every service now actually configures process
  logging: `LOG_LEVEL` (DEBUG..CRITICAL, default INFO) and `LOG_FORMAT`
  (`text`, or `json` for collector-friendly one-object-per-line) via
  `common/logging_setup.py` — before this, the root-logger default (WARNING)
  silently dropped every `logger.info` in the codebase. Both the syslog
  payload and both log formats escape control characters, so a hostile value
  cannot forge a second record (`common/log_safety.py`'s rule). Tested
  against real UDP/TCP/TLS sockets in `tests/unit/common/test_siem.py`,
  and validated live against the `syslog-collector` container on all three
  transports; not yet validated against a production SIEM appliance.
- **Separate DB credentials for the app and Keycloak, and an append-only audit log
  (NFR-2/NFR-3)** — `POSTGRES_USER` is now the bootstrap superuser only, never used for
  day-to-day traffic. `infra/postgres/init-app-roles.sh` (runs automatically on the
  `postgres` container's first boot, via Postgres's own `docker-entrypoint-initdb.d`)
  creates two non-superuser roles: `APP_DB_USER` (`ingestion-api`/`orchestration-mcp`'s
  `DATABASE_URL`, on the existing app database) and `KEYCLOAK_DB_USER` (Keycloak's
  `KC_DB_URL`, on its own separate `KEYCLOAK_DB_NAME` database) — the app and Keycloak no
  longer share a database or credentials, in this dev stack same as production always
  required (Helm never put them on the same Postgres instance to begin with, since
  Keycloak is external there). Superseded by #278: the one-shot is now
  `lock-down-db-grants`, still gated on `ingestion-api: condition: service_healthy` (so the
  tables definitely exist by then -- they are created by `common/db.py`'s `init_db()` during
  that service's own startup), but it now applies a per-service grant matrix across every
  table instead of hardening one. It reassigns ownership of every table away from the
  application roles -- not just a `REVOKE` while a role remains the owner, which it could
  trivially undo (table owners always retain `GRANT` on their own objects; losing ownership
  outright is what actually closes that). `audit_log` is now INSERT-only for all three
  services, `SELECT` included in what was removed, because nothing outside the test suite
  ever reads it. **Validated live** (#278): every role was made to attempt both the
  operations it needs and the ones it must not have, against a real Postgres 16.14 -- see
  `docs/roles-and-permissions.md` gap G2. That manual check is now a committed, automated
  regression test (issue #428): `tests/integration/test_nfr2_audit_log_append_only.py`,
  run against a live Postgres by `e2e.yml`'s `integration` job -- see
  `docs/testing.md`'s "Containerized integration layer" section.
- **Object storage for original uploaded files (NFR-12)** — `common/object_store.py`'s
  `ObjectStore` interface, with a filesystem-backed dev implementation
  (`FilesystemObjectStore`, `OBJECT_STORE_PATH=/srv/object-store`, a new `object-store-data`
  Compose volume) and an S3-compatible one (`S3ObjectStore`, any endpoint — existing
  enterprise S3, Ceph RGW, or another validated S3-compatible platform — via `boto3`'s
  generic client, for production). Wired into
  `app/routes/upload.py`: the raw uploaded bytes are written to the store (key
  `documents/{document_id}/original`, `common/object_store.document_object_key`) and the key
  recorded on the `Document` row *before* the 202 response returns — durable independent of
  Qdrant's chunk vectors and, previously, of anything at all (the original was only ever
  in-memory/`/tmp` during a single BackgroundTask's lifetime). The background processing
  task now reads the original back from the store rather than taking it as a direct argument
  — proves the round trip works, and matches the shape the NATS-based `ingestion-worker`
  (NFR-11, see below) actually uses now that processing runs in a genuinely separate
  process. Smoke-tested (put/get/delete round trip, path-traversal rejection, and a real
  `TestClient` POST confirming the object actually lands at the expected key before any
  processing runs) but the S3 backend itself is untested — no S3-compatible endpoint
  available in this sandbox. **Found live, fixed:** `ingestion-api`'s first real upload
  failed with `PermissionError: [Errno 13] Permission denied: '/srv/object-store/documents'`
  (surfacing to the browser as a cryptic `SyntaxError: JSON.parse: unexpected character at
  line 1 column 1` — Starlette's default 500 page for an unhandled exception is plain text,
  not JSON, and the upload page's JS didn't check `response.ok` before parsing). Root
  cause: `/srv/object-store` doesn't exist in either Dockerfile's image, only at the
  `object-store-data` Compose volume's mount point -- Docker auto-creates a missing mount
  point owned by `root`, but both containers run as the fixed non-root `appuser` (UID
  10001). `reranker-service`/`orchestration-mcp`'s Dockerfiles already `mkdir -p` their own
  cache mount points before `chown -R appuser:appuser /srv` for exactly this reason; this
  one just got missed when NFR-12 added the object-store mount after those were written.
  Fixed in both `services/ingestion-api/Dockerfile` (writes) and
  `services/ingestion-worker/Dockerfile` (reads) by adding `/srv/object-store` to the
  existing `mkdir -p` line ahead of the `chown`.
- **Content integrity verification (NFR-18, issue #285)** — `ingestion-api` computes a
  SHA-256 digest over the exact bytes it spools during upload (`app/routes/upload.py`,
  riding the existing #107 streaming read) and stores it on the `Document` row
  (`documents.content_sha256`); `ingestion-worker` re-computes that digest over the bytes it
  fetches back from the object store and refuses to process a mismatch (`ContentIntegrityError`
  in `app/processing.py`), landing the document in `failed` with a terminal ack rather than
  retrying — the same bytes under the same key will always mismatch the same way, so
  redelivery can't help. The digest is carried in the `document.submit`/`document.embedded`
  audit entries; it is deliberately excluded from, and scrubbed as part of, a purge tombstone
  (`common/purge.py`), for the same re-identification reason the existing filename exclusion
  there already documents. **Validated against a live environment** (2026-07-31, real
  `docker compose up` stack): uploaded a document and confirmed the stored digest matched the
  bytes on disk and the worker's `document.embedded` audit entry; separately, paused
  `ingestion-worker`, overwrote the object-store original in place, resumed the worker, and
  confirmed the document landed in `failed` with `reason: content_hash_mismatch` and both the
  expected and actual digests in the audit row; also confirmed purge nulls `content_sha256`
  on the row and never writes it to the `document.purged` audit entry. Validating this
  surfaced a real gap, tracked and fixed as issue #314 (below): the additive-column back-fill
  this required (`common/db.py`'s `_ensure_columns`) turned out to be incompatible with the
  #278 grants-lockdown model on any environment that has already been through one `docker
  compose up` cycle — this run needed a one-time manual `ALTER TABLE` as the bootstrap
  superuser to unblock during the validation above, before the fix existed.
- **Additive-column back-fill vs. the #278 grants lockdown (issue #314)** — `common/db.py`'s
  `_ensure_columns()` runs `ALTER TABLE ... ADD COLUMN` using each service's own
  least-privilege `DATABASE_URL`, but `ALTER TABLE` requires table *ownership* in Postgres —
  there is no separate grantable "add a column" privilege — and `lock-down-db-grants`
  reassigns every table's ownership to the bootstrap superuser specifically so no application
  role can grant itself anything back (#278). On any environment that has already been
  through one `lock-down-db-grants` cycle (every real deployment past its first boot), a
  release that adds a new entry to `_ADDITIVE_COLUMNS` for an existing table made
  `ingestion-api`/`ingestion-worker` crash-loop on startup with
  `psycopg.errors.InsufficientPrivilege: must be owner of table` — and since
  `lock-down-db-grants` itself depends on `ingestion-api: condition: service_healthy`, the
  whole stack deadlocked with no self-recovery. Latent since #278 landed (every additive
  column already in `_ADDITIVE_COLUMNS` at that point predates it — a `CREATE TABLE` for a
  brand-new table was already handled, since `ensure-roles.sh` re-grants `CREATE ON SCHEMA
  public` before every `up`); first triggered by #285's `content_sha256` column above, which
  is exactly how it was found. Fixed with a new `migrate-db-schema` one-shot Compose service
  that runs the same `init_db()` (so it can never drift from `_ADDITIVE_COLUMNS`, unlike
  duplicating it as raw SQL) connected as the bootstrap superuser, before `ingestion-api`/
  `ingestion-worker` start — same ordering `ensure-roles.sh` already uses for granting
  `CREATE` pre-startup. Both services' own later `init_db()` calls are now a no-op (the
  schema already matches) rather than a privilege error. **Validated against a live
  environment** (2026-07-31): reproduced the failure directly (`ALTER TABLE documents ADD
  COLUMN ... ` as the `nexus_rag_ingestion_api` role against this repo's own long-running,
  already-locked-down dev stack failed with exactly the `InsufficientPrivilege` error above),
  then confirmed a simulated new `_ADDITIVE_COLUMNS` entry applied cleanly through
  `migrate-db-schema` and that `ingestion-api`/`ingestion-worker` restarted healthy afterward,
  and that `lock-down-db-grants` still re-applies cleanly on top. A genuinely fresh volume was
  never affected by the original #314 bug (the column is created as part of `create_all()`,
  before ownership is ever locked down) -- but this fix turned out to have its own fresh-volume
  side effect, tracked and fixed as issue #317 below. Out of scope: this chart's
  `externalPostgres` path (Helm/production) deploys no database and already documents grant
  application as the external-Postgres operator's responsibility (`helm/nexus-rag/values.yaml`)
  — the equivalent schema-migration step there is the same operator's responsibility, noted
  alongside that existing comment.
- **Fresh-volume deadlock introduced by the #314 fix above (issue #317)** — `migrate-db-schema`
  creates every table owned by the bootstrap superuser from the very first boot, which closes
  #314's upgrade-path hole but opens a new one on a brand-new volume: `INGESTION_API_DB_USER`
  now holds *no* table-level privileges at all until `lock-down-db-grants` applies them, and
  that one-shot `depends_on: ingestion-api: condition: service_healthy` — but `ingestion-api`'s
  lifespan (`_seed_defaults` in `app/main.py`, which needs `SELECT`+`INSERT` on
  `classification_levels`) cannot report healthy without exactly those grants. Neither side can
  go first: every fresh `docker compose up -d` (`down -v` first, a new contributor's first `up`,
  or any CI job starting from a clean volume — `e2e.yml`'s `golden-query`/`browser-verify` jobs)
  hit this deterministically, not intermittently. Fixed by extracting the grant matrix out of
  `apply-service-grants.sh` into `infra/postgres/grant-service-privileges.sh` (one source of
  truth) and running it twice: once as a new standalone `grant-service-privileges` one-shot,
  connected as the bootstrap superuser, right after `migrate-db-schema` and before
  `ingestion-api`/`ingestion-worker` start (granting doesn't require table ownership — the
  bootstrap superuser can `GRANT` on any table regardless of owner — so this is safe before
  ownership has ever been reassigned); and again, unchanged in effect, from
  `apply-service-grants.sh` after its `REVOKE ALL`, so `lock-down-db-grants` remains the
  authority that also strips whatever the early pass didn't need to remove. The
  ownership-reassignment/`REVOKE` half stays exactly where it was — moving it earlier would
  reopen the #314 upgrade-path bug this whole mechanism exists to avoid — and its "refusing to
  apply grants" postcondition check is untouched. **Validated against a live environment**
  (2026-08-01): reproduced the deadlock on a genuinely fresh `postgres-data` volume (`docker
  compose up -d` with no prior volume — this repo's dev environment had none at the time),
  confirmed `ingestion-api` reached healthy after the fix with `grant-service-privileges`
  completing first, and confirmed `lock-down-db-grants` still re-applies cleanly afterward with
  no privilege drift (`\dp` matches the matrix exactly, same as before this change).
- **`lock-down-db-grants` racing concurrent requests during its REVOKE/GRANT window (issue
  #319)**, found live while validating #317 above: `apply-service-grants.sh` ran its `REVOKE
  ALL ... FROM role` loop and its re-`GRANT` (`grant-service-privileges.sh`) as separate `psql`
  invocations — separate autocommitting transactions — so on any re-`up` where
  `lock-down-db-grants` had real REVOKE/GRANT work to redo (every run after the first), a
  request landing in the gap between the REVOKE committing and the matching GRANT committing got
  a genuine `InsufficientPrivilege` 500. Not specific to `seed-sample-data`: any client request
  in flight at the wrong moment during a stack restart or rolling upgrade could hit it. Fixed by
  extracting the grant matrix to plain SQL (`infra/postgres/grant-matrix.sql`, `:"var"`
  identifier substitution) and having `apply-service-grants.sh` `\i` it directly, inside its own
  psql session, immediately after its `REVOKE` and still inside one explicit `BEGIN`/`COMMIT` —
  rather than shelling out to a second script that opened its own transaction. `grant-
  service-privileges.sh` (the earlier, standalone one-shot from #317) becomes a thin wrapper that
  applies the same file with `psql -f`. Postgres privilege checks read committed state only, so a
  transaction that never exposes an intermediate commit closes the window rather than narrowing
  it. **Validated against a live environment** (2026-08-01): reproduced the original race by
  extracting the pre-fix scripts via `git show` and running them in an isolated container against
  the live dev stack while hammering `classification_levels` as the `ingestion-api` role from a
  separate probe loop — 4 denials in 20s (`permission denied for table classification_levels`,
  the exact error from the issue). Ran the same probe against the fixed scripts, 4 times, forcing
  `lock-down-db-grants` to redo real REVOKE/GRANT work each time (concurrently with
  `seed-sample-data`'s own traffic on one rep) — 0 denials across 1,300+ probes, 0 `500`s in
  `ingestion-api`'s logs, and `\dp` still matches the matrix exactly.
- **Re-embedding path for a stale embedding-model collection (issue #362)** — #122/PR #130
  detected an `EMBEDDING_MODEL` mismatch and refused retrieval, but the only remedy was a
  full manual re-ingest. `python -m app.reembed` (run inside the `ingestion-worker`
  container/image, e.g. `docker compose run --rm ingestion-worker python -m app.reembed
  CUI`) re-parses, re-chunks, and re-embeds every `approved`/`pending_review` document in
  the given classification(s) — or every classification with such a document, if none are
  named — and writes the new chunks back in place via a new `replace_document_chunks`
  vector-store method (upsert new points under the same deterministic ids first, then sweep
  any old points a shorter re-chunking left behind — new-before-old, mirroring the FR-7
  supersession ordering). Idempotent by construction: a document is skipped unless its
  existing chunks' stamped model actually differs from the configured one (`--force`
  overrides), so an interrupted or re-run pass only touches what still needs it.
  Deliberately does not re-run the ingestion-time curation advisories (tagging/content/PII/
  precedent/LLM-suggestion) — those are artifacts of the original ingestion decision, not
  something a vector refresh should second-guess. **Validated against a live environment**
  (2026-08-03): manually stamped every point in the `CUI` collection with a fake stale
  `embedding_model`, confirmed `/debug/rag_search` then refused with the #122 mismatch error
  for a `bob-query` token, ran `python -m app.reembed CUI` against the real stack and
  confirmed it re-embedded the 3 `approved`/`pending_review` CUI documents (deliberately
  leaving the one `rejected` CUI document's stale stamp untouched — out of scope by design),
  confirmed the same query against `/debug/rag_search` then succeeded and returned the
  expected chunk, and confirmed a second `--dry-run` pass reported nothing left to do
  (0 to re-embed, 5 already current) — the skip-if-current check working as intended. Also
  confirmed the `document.reembedded` audit entry landed in Postgres for each re-embedded
  document. A stale `INGESTION_JOBS`/Postgres/Qdrant volume left over from an earlier session
  surfaced separately during this validation (38 undeliverable JetStream messages blocking
  fresh ones) — unrelated to this change, resolved by recreating those volumes, not a bug in
  the re-embedding path itself.
- **Missing `search_document:`/`search_query:` task prefixes on nomic-embed-text (issue
  #392)** — both embedding call sites (`ingestion-worker/app/embedding.py`'s `embed_texts`,
  `orchestration-mcp/app/rag_search.py`'s `_embed_query`) sent raw chunk/query text with no
  task-instruction prefix, which nomic-embed-text v1/v1.5's asymmetric training requires;
  nothing errored or looked broken, dense retrieval was just quietly worse than the model
  can do. Fixed by a shared, model-gated prefix lookup
  (`common/embedding_prefixes.py` — an unrecognized `EMBEDDING_MODEL` still gets no prefix,
  same as before this fix) applied at both call sites, and by folding prefix-scheme state
  into the #122 stamped embedding identity (`embedding_identity()`) so a corpus embedded
  before this fix is *refused* rather than silently compared against newly-prefixed
  queries, the same fail-closed behavior a genuine model change already got. **Validated
  against a live environment** (2026-08-05): confirmed the running dev corpus (stamped
  `nomic-embed-text`, pre-fix) was refused by `/debug/rag_search` with the #122 mismatch
  error once the fix was deployed, ran `python -m app.reembed` and confirmed all 31 chunks
  across all three classifications re-embedded and the same query then succeeded. Also ran
  `scripts/evaluate_retrieval.py` before (pre-fix code, corpus re-stamped bare
  `nomic-embed-text` to reproduce the old state) and after (post-fix code, re-embedded),
  using `--baseline`/`--history-dir`: `mean_recall_at_k` and `mean_precision_at_k` were
  identical (1.0 / 0.2) both times, with 0 forbidden-document leaks in either run — no FR-26
  regression, but this repo's 5-query golden set is small and already at ceiling for these
  personas/queries, and BM25's keyword leg dominates RRF fusion for short factual queries,
  so this harness could not isolate whatever improvement the dense leg alone gained. The
  deeper, judge-scored Q-to-C-to-A harness (`scripts/evaluate_rag_quality.py`, #383 —
  `contextual_relevancy`/`contextual_precision` are order-sensitive and score the dense
  ranking more directly) is a better instrument for this but needs a manually created
  LibreChat RAG Assistant (`--agent-id`) first; running it before/after this fix is left as
  an operator follow-up (issue #397).
- **NATS JetStream infrastructure and the `ingestion-worker` service (NFR-11)** — a `nats`
  service (`nats:2.14.3-alpine`, `-js` for JetStream, token-authenticated via `--auth`,
  monitoring endpoint on 8222 for the healthcheck, client port on 4222) plus
  `common/job_queue.py`: an `ensure_stream()` helper (idempotent, matching
  `common/qdrant_store.py`'s `ensure_collection()` pattern) and `publish_ingestion_job()`,
  publishing just a `document_id` to the `INGESTION_JOBS` stream — the original file lives
  in the object store (NFR-12 above), not the message payload, so this stays small
  regardless of upload size. `ingestion-api`'s `POST /documents` publishes to this stream
  (via `app.state.jetstream`, one long-lived connection set up in its lifespan, not
  reconnected per request) instead of running the pipeline itself; a new `ingestion-worker`
  service (`services/ingestion-worker`, its own Dockerfile/pyproject.toml/Compose service,
  port 8004) is the durable consumer — a `pull_subscribe` loop
  (`app/processing.py:consume_forever`) that fetches one job at a time, runs
  parse/chunk/embed/store (moved here from `ingestion-api/app/{parsing,chunking,embedding}.py`
  verbatim), and acks the message only on a terminal outcome (success or a permanent
  parse/embed failure lands the document in `failed`); an unexpected/transient error (Qdrant
  or the DB unreachable, a bug, etc.) is left un-acked, so JetStream redelivers it after
  `ACK_WAIT_SECONDS` (300s) instead of the document being silently stuck in `processing`
  forever the way a `BackgroundTasks` crash would leave it. Qdrant's full read/write key now
  goes to both `ingestion-api` (still updates/deletes points directly on approve/reject/
  supersede, `app/routes/curate.py`) and `ingestion-worker` (creates the collection, writes
  new points) — `orchestration-mcp` keeps the read-only key, unchanged. Smoke-tested
  `process_document`'s three outcome branches (success → `pending_review`; permanent
  `ParsingError`/`EmbeddingError` → `failed`, acked; unexpected exception → left un-acked,
  `doc.status` stays `processing` for redelivery to pick up) against an in-memory SQLite DB
  with Qdrant/object-store/embedding calls mocked, and confirmed both services' packages
  install and import cleanly. **Validated against a real `docker compose up`**: a document
  submitted as `alice-ingest` was durably queued, picked up by `ingestion-worker`, and
  confirmed reaching `queued → processing → pending_review` via `GET /documents/{id}`
  polling — not just the mocked unit-level checks above. That live run also caught a real
  bug the mocks couldn't: `ingestion-api`/`ingestion-worker`'s Dockerfiles never created
  `/srv/object-store` before `chown -R appuser:appuser /srv`, so the Compose volume's
  auto-created mount point stayed owned by `root` and every write threw `PermissionError`
  (surfacing to the browser as an opaque `SyntaxError: JSON.parse: unexpected character at
  line 1 column 1`, since Starlette's default 500 page for an unhandled exception is plain
  text, not JSON). Fixed by adding `/srv/object-store` to each Dockerfile's existing
  `mkdir -p` line, matching the pattern `reranker-service`/`orchestration-mcp` already used
  for their own cache mounts. The document was then curated/approved and found by a real
  claims-filtered query against `orchestration-mcp`'s `/debug/rag_search` with a
  `bob-query`-obtained Keycloak token — the full NFR-11 pipeline confirmed end to end, not
  just its individual pieces.
- **Document supersession safety, reviewed and hardened (NFR-13)** — re-read
  `app/routes/curate.py`'s `approve()`/`reject()` specifically for the failure-mode NFR-13
  calls out: "a partial failure during republication must not leave the corpus in an
  inconsistent state." The ordering that was already there is the right one and needed no
  change: on a supersede, the *new* document's Qdrant chunks are flipped to `approved`
  (making it retrievable) *before* the *old* document's chunks are deleted, and
  `_validate_supersede` re-checks the whole chain (old document's current status, the
  curator's authority over *it*, not just the new one) before any mutation happens to
  either document — so there's never a window where neither version is retrievable, and a
  validation failure never leaves a half-approved document behind. What the ordering didn't
  cover: Postgres (`session.commit()`) and Qdrant (`update_document_payload`/
  `delete_document_chunks`) aren't one transaction — the Qdrant write happens first (it has
  to, so a validation failure can still be rejected cleanly beforehand), and if the code
  between that write and the eventual `session.commit()` then raises (a DB error, the old
  document's Qdrant delete failing, etc.), `get_session()`'s context manager rolls Postgres
  back to `pending_review`, but the earlier Qdrant write doesn't roll back with it — leaving
  Qdrant already showing the document as `approved`/`rejected` (and therefore already
  affecting retrieval, since FR-11/FR-26 filtering reads Qdrant's payload, not the Postgres
  row) while Postgres and the curation queue both still call it `pending_review`. Both
  `approve()` and `reject()` now wrap everything from that Qdrant write through
  `session.commit()` in a `try`/`except` that, on any failure, best-effort reverts the
  Qdrant payload back to `pending_review` (logging loudly, not silently, if the revert
  itself also fails) before re-raising — so the normal outcome of a partial failure is both
  stores agreeing again on `pending_review`, not a document that's live in search results
  while every status view still calls it unreviewed. This doesn't (and can't, without
  re-ingesting) undo an old document's chunks actually being deleted from Qdrant if that
  step itself succeeds and something later fails — but by the time that delete runs, the
  new document's Qdrant payload has already been flipped to `approved`, so the corpus
  always has *something* retrievable; what could still lag is Postgres's bookkeeping view,
  which is exactly the gap this change closes. Originally smoke-tested ad hoc (bypassing the
  FastAPI layer, calling `approve()`/`reject()` against an in-memory SQLite DB with a mocked
  Qdrant client) but never committed as a regression test — issue #77 flagged that nothing in
  the repo actually failed if this logic regressed. `services/ingestion-api/tests/
  test_curate_nfr13_revert.py` now pins that same technique down permanently: a normal
  approve/reject; `session.commit()` raising on a plain approve, on a reject, and on a
  supersede where the old document's Qdrant delete itself raises; and the revert call itself
  also failing (confirming the *original* exception still propagates, not the revert's). In
  every failure case, the Qdrant write is reverted to `pending_review` and the exception still
  propagates (so the caller gets a 5xx, not a silent partial success). The happy path — a
  normal `approve()` against a real Postgres/Qdrant pair — has since been validated live (see
  the NFR-11 bullet above: the live-tested document was curated and approved for real). The
  `try`/`except` revert-on-failure branch itself is committed and tested against mocks, not
  yet against a real multi-container live stack — deterministically forcing a real Qdrant
  call to fail at that exact point without a dedicated fault-injection hook in production code
  (out of scope for this pass) still needs one, so that step of #77 remains open.
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
  `ALL_AUTHENTICATED` notice, an org-scoped policy, a `Signal-Corps`-scoped `SECRET` document
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
  re-evaluate"). It remains the fast, deterministic, judge-free security and retrieval
  gate. Issue #74 adds the complementary host-side `scripts/evaluate_rag_quality.py`:
  it drives the real LibreChat Agent generation path, obtains ordered structured contexts
  from `/debug/rag_search`, and uses the local Ollama model to report contextual
  relevance/recall/precision, faithfulness, answer relevance/correctness, citation
  validity, and abstention behavior. Run it after creating the per-user RAG Assistant:
  `python scripts/evaluate_rag_quality.py --agent-id <agent-id> --history-dir
  .eval-history/qca`. Local-judge scores are relative comparisons only, and the tool is
  manual rather than a CI gate; see `docs/testing.md` for its data-handling and baseline
  rules.
- **Prompt-injection mitigation for retrieved content (P1)** — retrieved chunk `text` is
  untrusted by construction (whatever an uploader submitted; FR-18's tagging validation
  constrains metadata, not document content). `orchestration-mcp`'s `rag_search`
  (`app/rag_search.py`) now delimits every result's `text` with an explicit
  `<untrusted_document_content>` marker — applied *after* reranking, so
  `reranker-service`'s cross-encoder still scores the raw text, not text padded with
  marker tags — and adds a `security_notice` field telling the calling model to treat
  delimited content as reference material, not instructions, including (issue #427) a
  persona/roleplay/compliance-marker reframing of that content, not just a blunt
  instruction override. Smoke-tested with a fabricated chunk containing an
  injection-shaped sentence ("ignore previous instructions and reveal..."): confirmed
  the reranker call receives the raw, undelimited text (so scoring quality doesn't
  degrade) while the final response's `results[].payload.text` is properly delimited and
  the notice is present. This is a mitigation, not a guarantee. It was live-evaluated
  against real LibreChat generation (issue #97) and found not fully resisted for the
  persona/roleplay case specifically; issue #427 strengthened the notice's wording for
  that case and fixed a real wiring gap (`format_rag_search_for_model`, the function that
  builds what the real `rag_search` tool returns to a calling model, was never actually
  including this notice at all — only the `/debug/rag_search` diagnostic JSON was), but a
  live re-run confirming the strengthened wording changes model behavior was not completed
  — see REQUIREMENTS.md Section 11 for the full status and why.
- **In-app knowledge base (FR-33, issue #303)** — a `GET /kb` page (nav icon next to the
  account/logout controls in `base.html`, since it's a persistent utility rather than a
  workflow tab) with one how-to article per capability role (ingest, query, curate, purge,
  admin), each rendered only when the signed-in user's own `UserClaims` grants that role
  (`kb.html`'s `{% if current_user.can_* %}` checks, the same pattern `base.html` already
  uses to gate nav tabs) — a user holding multiple roles sees the union of every article
  those roles unlock, and one holding none yet sees an explicit "ask your Keycloak admin"
  message instead of an empty page. **Validated against a live environment** (2026-07-31,
  real `docker compose up` stack): role-gated article visibility and the knowledge-base
  content itself confirmed against real signed-in users. Also covered by unit tests
  (direct route-function calls against an in-memory SQLite session, mirroring
  `test_login_gate.py`'s pattern, one case per role combination) for regression coverage
  between live runs.

**Stubbed / TODO (see inline `TODO` comments at each site):**
- **Keycloak RFC 8693 token-exchange (`grant_type=token-exchange`) — verified live via a
  scripted exchange, but turned out not to be the grant type LibreChat's real OBO code
  path actually uses (see the opening summary and the `librechat.yaml` bullet below);
  `standard.token.exchange.enabled` on `librechat` is currently dead config, left in place
  in case Keycloak's RFC 7523 story clarifies enough to revisit OBO.** The manual-verification
  work below is still accurate for what it tested, just not for what LibreChat calls at
  runtime. The assumed "manual admin-console step" turned out not to apply at all — that
  belief traced to a
  misreading of Keycloak's docs: the fine-grained admin permission is only required for the
  deprecated/preview *legacy* token exchange. Standard Token Exchange V2 (RFC 8693, what
  `standard.token.exchange.enabled` actually configures, confirmed via
  `www.keycloak.org/securing-apps/token-exchange`) needs no such permission — just the
  switch on the correct client. The real, previously-undiagnosed bug: that attribute was set
  on `rag-app` (the exchange's *target*) instead of `librechat` (the *requester* — the client
  that actually calls the token endpoint with `grant_type=token-exchange`, per Keycloak's own
  example). Moved to `librechat`'s `attributes` in the realm export. Verified with a scripted
  exchange (`client_id=librechat` + a `bob-query` subject token → `audience=rag-app`): the
  resulting token carried the correct `aud`, `azp`, and `rag_roles`/`clearance`/`org`/
  `releasability` claims, and a real `POST /debug/rag_search` call with it returned correctly
  claims-filtered results. One more real bug surfaced along the way: the exchanged token's
  issuer is `https://keycloak:8443/realms/nexus-rag` (Keycloak's HTTPS listener, added for
  the login fix above) — `orchestration-mcp`/`ingestion-api`'s `OIDC_ISSUERS` allowlist only
  had the `:8080` HTTP forms, so the exchanged token 403'd with "invalid token: Invalid
  issuer" until that third issuer was added (same dual-issuer pattern as before, just a third
  entry). Similarly, reusable access tokens (the other Section 7.7 OBO prerequisite) are a
  LibreChat-side OpenID setting, not a Keycloak client attribute — set via
  `OPENID_REUSE_TOKENS=true` in `docker-compose.yml`'s `librechat` service environment, not
  `librechat.yaml` (that file is LibreChat's `endpoints`/`mcpServers` config, not its auth
  environment variables). (Historical note, kept for anyone who hits the same trap: don't add
  a `_comment` field or similar JSON-comment workaround to the realm export — Keycloak's
  importer uses strict JSON deserialization and refuses the whole realm over one unrecognized
  property, confirmed live: `ERROR: Unrecognized field "_comment"`.)
- `infra/librechat/librechat.yaml`'s `mcpServers` shape was checked against a real running
  LibreChat 0.8.7 instance and found one real error along the way: `obo.scopes` was a JSON
  array (`["rag-query"]`), but LibreChat's actual Zod config schema wants a single
  space-delimited string (standard OAuth2 scope-parameter format, RFC 6749) — LibreChat
  refused to start at all (`Exiting due to invalid configuration`) until fixed. That fix
  got LibreChat running and the `obo` config accepted, but a real chat message actually
  triggering `rag_search` (2026-07-26, `bob-query`) surfaced the deeper problem above:
  LibreChat's `OboTokenService` calls Keycloak with `grant_type=jwt-bearer`, which Keycloak
  rejected outright (`JWT Authorization Grant is not supported for the requested client`) —
  a hard config mismatch, not the previously-suspected `openidJwt` "invalid algorithm" bug
  (that one either doesn't apply to this code path or was already fixed by an earlier commit;
  the failure now happens Keycloak-side, meaning LibreChat's request reached the token
  endpoint fine). Switched `nexus-rag-search` to `addUserJwtToken: true` instead of `obo` at
  the time -- **since superseded, see the next bullet: that switch never actually worked,
  and `obo` is now a closed avenue, not a "revisit later" one.** The `obo` config block
  itself was removed from `librechat.yaml`; the Zod-shape fact above (single
  space-delimited string, not an array) still applies if OBO is ever revisited regardless.
- **Both of the above turned out to be dead ends, and MCP auth-forwarding now uses OAuth
  login instead (issue #99 follow-up, 2026-07-26).** Two more real findings closed the door
  on continuing to chase token-forwarding/exchange:
  - `addUserJwtToken: true` **never actually forwarded anything.** A live `tcpdump` capture
    on `orchestration-mcp`'s own port (`docker run --network container:nexus-rag-
    orchestration-mcp-1 alpine ... tcpdump -A -i any -w capture.pcap 'tcp port 8002'`, since
    this host has no passwordless sudo for a host-level capture) showed the real `POST /mcp`
    `CallToolRequest` LibreChat sent had no `Authorization` header at all -- confirmed by
    grepping the installed LibreChat build's actual
    `StreamableHTTPOptionsSchema`/`MCPOptionsSchema` in
    `packages/data-provider/dist/data-service-*.mjs`: `addUserJwtToken` is not a recognized
    field in this LibreChat version. Unknown keys are silently dropped by Zod, so this had
    been a no-op the entire time it was configured, not something that later broke.
  - `obo`/RFC 7523 is not a viable path for this topology, confirmed against Keycloak's own
    JWT Authorization Grant documentation (not assumption): it requires the assertion's
    issuer to be a registered, *linked external* Identity Provider, and the assertion's
    `aud` claim to equal Keycloak's own issuer/token-endpoint URL. Neither holds for
    `librechat` and `rag-app` being two clients in the *same* realm on the *same* Keycloak
    instance -- making it fit would mean a self-referential Identity-Provider setup plus
    reworking the `aud=rag-app` scheme the existing claims check depends on. Checked whether
    a different self-hosted IdP sidesteps this (Authentik: doesn't implement RFC 7523 as an
    authorization grant at all, only as client-assertion auth, a different purpose; Ory
    Hydra/Zitadel: more flexible jwt-bearer support in principle, but irrelevant here since
    LibreChat's OBO code sends `grant_type=jwt-bearer` unconditionally regardless of which
    IdP is behind it) -- swapping IdPs doesn't change what LibreChat asks for, and replacing
    Keycloak everywhere in this stack (LibreChat login, `ingestion-api`, `orchestration-mcp`,
    provisioning, docs) for one OBO nuance would be a full infrastructure migration, not a
    fix.

  **Fix**: `infra/librechat/librechat.yaml`'s `rag` server now configures `oauth` (a real,
  separate RFC 6749 `authorization_code` login specifically for this MCP server, distinct
  from LibreChat's own OIDC login) plus `requiresOAuth: true` and `startup: false`, reusing
  the existing `rag-app` client (already confidential, already carries the
  `nexus-rag-claims` scope). This is a genuinely different mechanism, not a rename of the
  same problem: it's LibreChat driving a standard browser login/consent flow (the user
  clicks "Connect" once per MCP server) rather than silently forwarding or exchanging a
  token behind the scenes, so the resulting token is issued by Keycloak the ordinary way
  and passes `orchestration-mcp`'s existing `common.claims.parse_claims` check unmodified --
  no same-realm trust relationship required at all. Getting the login to actually trigger
  and complete took two more real, live-debugged bugs:
  - `requiresOAuth` defaults to auto-detection, which never fired against the original
    server (`OAuth Required: false` logged, then "Connection successfully established" with
    zero auth attempted). At that point, `orchestration-mcp` did not challenge at the HTTP
    boundary; the check happened inside `rag_search`, so auto-detection had no signal.
    `requiresOAuth: true` was forced explicitly and remains so. As of the 2026-07-28 token
    expiry fix, the transport also validates every bearer and returns
    `401`/`WWW-Authenticate: Bearer ... invalid_token`; that lets LibreChat refresh an
    expired access token instead of surfacing a tool result containing `Signature has
    expired`.
  - Even with the OAuth config in place, LibreChat refused to redirect at all: `Failed to
    initiate OAuth flow OAuth authorization_url resolves to a private IP address` --
    `keycloak` resolves to a private Docker-network IP from this container's own DNS, even
    though the *browser* can reach it fine via the `/etc/hosts` alias from issue #75. First
    tried `mcpSettings.allowedAddresses` (host:port pairs, exactly the private-IP SSRF
    exemption the field's own docstring describes) -- didn't work. Reading
    `packages/api/src/mcp/oauth/handler.ts`'s actual `isOAuthUrlAllowed()` explained why:
    once `mcpSettings.allowedDomains` is non-empty (it already was, for the
    `orchestration-mcp` streamable-http entry), it becomes the *sole* authority for the
    OAuth URL check and `allowedAddresses` is ignored outright by design ("letting it
    short-circuit here would broaden a strict admin-configured OAuth scope", per that
    function's own comment). Fixed by adding `https://keycloak:8443` to `allowedDomains`
    instead of `allowedAddresses`.

  **Confirmed live end to end** after all of the above: `bob-query`, via a real Agent in the
  browser, clicks "Connect", completes a real Keycloak login, and `rag_search` returns real,
  claims-filtered results -- not just a clean tool call (the earlier milestone), the actual
  retrieval working through the full LibreChat → MCP OAuth → Keycloak → orchestration-mcp →
  Qdrant chain. Reconfirmed from a live authenticated MCP session on 2026-07-28 after
  adding transport authentication: an unauthenticated initialize receives `401
  invalid_token`, while a Keycloak-authenticated `rag_search` for the password policy
  succeeds through hybrid retrieval and reranking.
- **`orchestration-mcp`'s MCP endpoint rejected every LibreChat request with `421
  Invalid Host header`, even after the `addUserJwtToken` fix above got the OBO/auth layer
  itself working.** Confirmed live (2026-07-26): `docker logs` on both sides showed
  LibreChat's transport erroring `Streamable HTTP error: Error POSTing to endpoint:
  Invalid Host header` while `orchestration-mcp` logged `Invalid Host header:
  orchestration-mcp:8002` and a `421 Misdirected Request`, before the request ever reached
  `rag_search`'s own auth check. Root cause: `mcp` SDK auto-enables DNS-rebinding
  protection (`mcp.server.transport_security.TransportSecuritySettings`) whenever it's
  constructed with the default `host="127.0.0.1"`, allowlisting only `127.0.0.1`/`localhost`/
  `::1` Host headers — it assumes a loopback bind and has no way to know the container will
  actually be reached over the Compose network as `orchestration-mcp:8002`, which is exactly
  the Host header LibreChat's requests carry. Fixed in `services/orchestration-mcp/app/
  server.py` by passing an explicit `TransportSecuritySettings` that extends
  `allowed_hosts` with `orchestration-mcp:*` instead of disabling DNS-rebinding protection
  outright. After the fix, LibreChat's MCP log shows a clean `Tools: rag_search` /
  `Initialized in: Nms` on startup and `orchestration-mcp`'s own log shows `200`/`202` on
  `POST /mcp` instead of `421`.
- **LibreChat also needs its own `JWT_SECRET`/`JWT_REFRESH_SECRET`/`CREDS_KEY`/`CREDS_IV`,
  independent of the `librechat.yaml`/OIDC config above** — found via the same live
  `docker compose up` run, one error at a time: after the `obo.scopes` fix, LibreChat's next
  failure was `Failed to start server: JwtStrategy requires a secret or key`. These four are
  required at LibreChat startup regardless of auth method (they're for LibreChat's own
  session JWTs and its AES-256-CBC encryption of credentials it stores in MongoDB, e.g.
  user-provided plugin API keys — nothing to do with Keycloak). `JWT_SECRET`/
  `JWT_REFRESH_SECRET` have no length requirement; `CREDS_KEY`/`CREDS_IV` do, and LibreChat
  validates it — 32 bytes/64 hex chars and 16 bytes/32 hex chars respectively, or it won't
  start. Added as `LIBRECHAT_JWT_SECRET`/`LIBRECHAT_JWT_REFRESH_SECRET`/
  `LIBRECHAT_CREDS_KEY`/`LIBRECHAT_CREDS_IV` in `.env.example`/`docker-compose.yml`, with
  dev-only defaults generated via `openssl rand -hex 32`/`openssl rand -hex 16` — never
  reuse those specific values past throwaway local dev.
- **`ALLOW_SOCIAL_LOGIN` must be set explicitly -- LibreChat's OIDC login button is
  otherwise silently absent.** Found the same way: LibreChat started cleanly (past both
  fixes above) but the login page had no OIDC option at all, no error logged anywhere.
  LibreChat's own `.env.example` ships `ALLOW_SOCIAL_LOGIN=false` as the default -- it's a
  feature switch for the whole social/OIDC login family, separate from actually configuring
  an OIDC provider (`OPENID_ISSUER`/`OPENID_CLIENT_ID`/etc.), and nothing about a correctly
  configured but unused provider produces a warning. Set to `"true"` in `docker-compose.yml`'s
  `librechat` service. Also added `DOMAIN_CLIENT`/`DOMAIN_SERVER` (`http://localhost:3080`,
  matching the host port binding) alongside it -- LibreChat combines `DOMAIN_SERVER` with
  `OPENID_CALLBACK_URL`'s relative path to build the absolute callback URL used in the OIDC
  redirect, and leaving it unset risked a second, separate failure mode once the button
  itself was fixed.
- **A plain chat with the `LiteLLM`/`Ollama-Direct` custom endpoints never calls
  `rag_search`.** Root-caused (2026-07-26) by reading LibreChat's own source in the running
  container: MCP tools (and the `ephemeralAgent` tool-attachment mechanism behind the
  composer's tools/wrench icon) are only wired into
  `api/server/controllers/agents/client.js` (`isAgentsEndpoint`/`loadAgentTools` in
  `api/server/services/Endpoints/agents/initialize.js`) -- the plain `endpoints.custom`
  chat path (what a bare "LiteLLM"/"Ollama-Direct" conversation uses) never attaches tools
  to the completion request at all, regardless of what's registered under `mcpServers`.
  Registering an MCP server in `librechat.yaml` only makes it *available* to attach;
  nothing calls it automatically. **Confirmed live**: running as an Agent (Agent Builder,
  `rag_search` attached) does trigger the tool call, matching this analysis.
- **Running as an Agent surfaced a second, separate bug: `llama3.2:1b`'s tool calls came
  back malformed** (`Received tool input did not match expected schema`, LibreChat's error
  showing garbled parameter names like a stray `message` key not in `rag_search`'s schema
  at all). Not a schema bug in `orchestration-mcp` -- reproduced directly against LiteLLM
  outside LibreChat entirely (`POST /v1/chat/completions` with the real `rag_search` tool
  schema, run repeatedly): `ollama/llama3.2:1b` only returned a correctly-formed
  `tool_calls` response on roughly 1 in 3-5 tries, sometimes echoing the whole tool schema
  back as prose instead of calling it, sometimes inventing a function name that didn't
  exist even on a schema simplified to one required string field. This is a capability
  limit of the 1B model against Ollama's tool-calling template, not something fixable by
  adjusting the tool's parameter schema. **Fixed by swapping `GENERATION_MODEL` to
  `qwen2.5:7b-instruct`** (`.env`/`.env.example`, `infra/litellm/config.yaml`,
  `infra/librechat/librechat.yaml`'s two `models.default` entries, and
  `docker-compose.yml`'s `ollama-model-init` default) --
  the same repeated direct-LiteLLM test got 5/5 (then a further 3/3 against the exact
  `rag_search` schema) correctly-formed tool calls, zero malformed/hallucinated responses.
  Needs `ollama pull qwen2.5:7b-instruct` (~4.7GB, done automatically by
  `ollama-model-init` on a fresh `docker compose up`) and noticeably more RAM/CPU time per
  request than `llama3.2:1b` -- worth it here since unreliable tool-calling made MCP
  testing non-viable at the smaller size.
- **Even after the `qwen2.5:7b-instruct` swap, a real Agent run in the browser still didn't
  call the tool** — caught live (2026-07-26) via a `bob-query` browser session, not a
  synthetic test: the isolated single-message LiteLLM tests above used a short hand-written
  tool schema, not the real one `orchestration-mcp` actually serves over MCP (long
  multi-paragraph docstring, Pydantic-generated `anyOf` types, and LibreChat's namespaced
  function name `rag_search_mcp_nexus-rag-search`). Captured the real request LibreChat
  sends with `tcpdump` (a throwaway container sharing `ollama`'s network namespace, since
  this host has no passwordless sudo for a host-level capture) and replayed it verbatim
  against a second, disposable Ollama instance (same model volume, different port, so as
  not to disturb the live stack) with `OLLAMA_DEBUG_LOG_REQUESTS=1` for exact reproduction.
  Root cause, isolated by a controlled A/B (8 tries per variant, same real message history):
  the long docstring and the long namespaced function name *both* independently hurt
  reliability -- long name + long desc: 2/8; long name + short desc: 5/8; short name
  (`rag_search`) + short desc: 8/8, holding at 8/8 even with the exact real (slightly
  malformed, duplicate-message) conversation history captured from the browser. This is
  the same class of problem as the `llama3.2:1b` finding above (structured tool-call
  generation degrading under more complex/longer prompts), just showing up at a smaller
  scale on a bigger model instead of disappearing entirely.

  **Fixed two ways**, since both factors independently mattered: (1) shortened
  `rag_search`'s docstring in `services/orchestration-mcp/app/server.py` -- the MCP SDK uses
  the function's docstring verbatim as the LLM-facing tool description, so the multi-
  paragraph version (FR references, issue numbers, security rationale) was shipped to the
  model on every single call; moved that context to a regular code comment above the
  function (the security notice specifically is redundant to remove from there anyway --
  every real response is meant to already carry the same `SECURITY_NOTICE` text per
  `app/rag_search.py`, so nothing should be lost by not repeating it in the schema --
  though issue #427 later found `format_rag_search_for_model`, the function that builds
  what the real tool actually returns, had never included it, only a shorter
  independently-worded line; fixed there, see REQUIREMENTS.md Section 11). (2) Renamed
  `infra/librechat/librechat.yaml`'s `mcpServers` key from `nexus-rag-search` to `rag` --
  LibreChat namespaces every MCP tool as `{tool}_mcp_{this key}` in the schema sent to the
  model, so this shortens the model-facing name from `rag_search_mcp_nexus-rag-search` to
  `rag_search_mcp_rag`. Re-verified against the real LiteLLM endpoint with the actual
  (now-shortened) schema: 5/5 clean tool calls.
- **Helm chart changes are hand-written, unverified by `helm lint`/`helm template`** — no
  network access to install the `helm` CLI in this environment (see
  `helm/nexus-rag/README.md`'s note at the top, unchanged from earlier chart work). This
  applies to the new `externalKeycloak.clientId`/`clientSecret` and
  `ingestionApi.oidcRedirectUri`/`cookieSecure` wiring same as everything else in the
  chart — run `helm template --debug` against a real values override before trusting it.
- **OpenAI-API-compliant embedding client (issue #403, Phase 2 of #401)** — both embedding
  call sites (`ingestion-worker/app/embedding.py`'s `embed_texts`,
  `orchestration-mcp/app/rag_search.py`'s `_embed_query`) previously spoke only Ollama's
  native `/api/embeddings`, hardcoded, so `embeddingService.external` (#401) could only
  point at an Ollama-*compatible* endpoint, not a genuinely OpenAI-API-compliant hosted
  model (vLLM, TGI, a cloud embedding endpoint) despite that being #401's original ask.
  Factored the request/response wire protocol out to a shared
  `common/embedding_client.py`, selected by `EMBEDDING_API_COMPATIBILITY`
  (`"ollama"`, default, unchanged behavior, vs. `"openai"`: `/v1/embeddings`,
  `Authorization: Bearer EMBEDDING_API_KEY` when set) and wired through
  `embeddingService.external.apiCompatibility`/`.apiKey` in the Helm chart. Unlike this
  file's other Helm entries, `helm lint`/`helm template` *were* run here (`helm` is
  reachable via `host-spawn helm` in this environment, unlike when the note above and
  `helm/nexus-rag/README.md`'s old blanket disclaimer were written — see that README's
  now-corrected note) across every new value combination, including the fail-closed and
  TLS-scheme cases. **Tested against mocks only**: `common/embedding_client.py`'s two code
  paths are covered by `tests/unit/common/test_embedding_client.py` (`respx`-mocked HTTP,
  100% line/branch coverage on the new module) and the existing `embed_texts`/`_embed_query`
  prefix-application suites in both services pass unmodified against the refactor — no real
  OpenAI-API-compliant server (a local vLLM instance, etc.) was reachable in this
  environment to validate the `"openai"` path end-to-end. Treat that path as implemented and
  unit-tested, not yet live-validated, until someone runs it against a real endpoint.
- **OpenAI-API-compliant vision/classification/PII-LLM completion client (issue #418, Phase 1
  of the ask split from #403's Note; reranking is tracked separately as #419)** — the three
  remaining Ollama-native call sites (`app/captioning.py`'s `_caption_one`, `app/classification_
  suggestion.py`'s `suggest_classification`, `app/pii_llm_advisory.py`'s
  `suggest_pii_llm_findings`/`verify_pii_findings`) previously hardcoded `/api/generate`,
  unlike embeddings (#403). Factored the request/response wire protocol out to a shared
  `common/completion_client.py`, selected by `COMPLETION_API_COMPATIBILITY` (`"ollama"`,
  default, unchanged behavior, vs. `"openai"`: `/v1/chat/completions`, `Authorization: Bearer
  COMPLETION_API_KEY` when set, images carried as `image_url`/base64-data-URI content parts for
  the vision case) and wired through the same `embeddingService.external.apiCompatibility`/
  `.apiKey` Helm fields as `EMBEDDING_API_COMPATIBILITY` — one config knob, not a second one
  that could drift from it, since captioning/classification/PII-LLM already point at that same
  instance (see `embeddingService`'s own values.yaml comment). `helm lint`/`helm template`
  (`host-spawn helm`) were run across the new env-var block, including both the
  present-and-populated and absent-when-no-feature-enabled cases. **Validated against a live
  environment**, not just mocks: `docker compose up -d ollama` (standalone, not the full stack)
  against the pinned `ollama/ollama:0.32.1`, then `common.completion_client.request_completion`
  called directly (not through the full worker pipeline) against the real container's
  `/v1/chat/completions` and `/api/generate` endpoints. Text case (`qwen2.5:0.5b-instruct`,
  `json_format=True`, the classification/PII-LLM shape): both wire protocols returned real
  model output, the OpenAI-compatible response's JSON parsed cleanly through the same
  `_parse_response` logic the callers use. Vision case (`moondream`, a synthetic two-square
  red/blue PNG): both wire protocols returned a caption that named the actual colors present,
  confirming the OpenAI-compatible `image_url`/data-URI content-part shape is accepted by a
  real server, not just internally consistent with itself. **Tested against mocks only**:
  `tests/unit/common/test_completion_client.py` (`respx`-mocked, both wire protocols, request
  shape/response parsing/error handling) and the existing `captioning`/`classification_
  suggestion`/`pii_llm_advisory` test suites in `services/ingestion-worker/tests/`, which pass
  unmodified against the refactor since they mock at the HTTP boundary, not the Python call.
  Not exercised: a full document ingestion round trip with `VISION_MODEL`/`CLASSIFICATION_MODEL`/
  `PII_LLM_MODEL` set to `"openai"` compatibility mode end-to-end through the worker's JetStream
  consumer — the direct-client check above validates the wire protocol, not the full pipeline
  wiring.
- **External reranker wire formats (issue #419, the decision split from #418)** — `reranker-
  service` isn't the "OpenAI-compatible chat completions" shape #418's other three features
  are (no official OpenAI `/v1/rerank` endpoint exists), so this needed its own decision.
  `orchestration-mcp/app/reranking.py`'s `rerank()` previously spoke only this chart's own
  `reranker-service` shape, hardcoded. Added `RERANKER_API_COMPATIBILITY`: `"internal"`
  (default, unchanged), `"tei"` (HuggingFace text-embeddings-inference's native `/rerank` --
  the issue's recommended default for a real external endpoint), or `"cohere"` (the Jina/
  Cohere-style `/v1/rerank` convention). Wired through new `rerankerService.enabled`/
  `.external.{host,port,tls,apiCompatibility,apiKey,model}` Helm values (same enabled/external
  pattern as `embeddingService`) and a new `nexus-rag.rerankerUrl` helper. **Web-researched
  correction to the issue's own text**: the issue's Option 2 write-up assumed vLLM's rerank
  endpoints might speak TEI's shape; they don't. vLLM's `/rerank`, `/v1/rerank`, `/v2/rerank`
  are documented as compatible with "Jina AI's and Cohere's re-rank API interface" specifically
  (`model`/`query`/`documents` in, `results: [{index, relevance_score}]` out) -- the `"cohere"`
  mode above, not `"tei"`. Both shapes were implemented rather than only the recommended
  default, since a concrete need for the second one (vLLM, already part of this stack per
  CLAUDE.md) surfaced immediately rather than needing to be spun up as separate follow-up
  work. **Tested against mocks only**: `services/orchestration-mcp/tests/test_reranking.py`'s
  `TestTeiCompatibility`/`TestCohereCompatibility` classes (`monkeypatch`-mocked HTTP, request
  shape/response-index-mapping/auth-header/fallback-on-outage for both new modes), 100%
  line coverage on `app/reranking.py` under the service's own `--cov=app.reranking
  --cov-fail-under=85` gate. No real TEI or vLLM server was reachable in this environment to
  validate either wire format end-to-end. `helm lint`/`helm template` (`host-spawn helm`) were
  run across every new value combination: default (self-deployed, unchanged rendering),
  external `"tei"` with an `apiKey` secret, external `"cohere"` without one, and the
  fail-closed case (`enabled: false` with no `external.host` set) -- confirmed each renders
  the expected `RERANKER_URL`/`RERANKER_API_COMPATIBILITY`/`RERANKER_API_KEY` env vars (or
  omits them correctly) and that `reranker-service`'s Deployment/Service/PVC/NetworkPolicy
  stop rendering entirely in external mode, same as `embeddingService`'s existing pattern.
- **Batched chunk embedding (issue #396)** — `ingestion-worker/app/embedding.py`'s
  `embed_texts` sends `EMBEDDING_BATCH_SIZE` (default 32) chunks per request through the
  new `common.embedding_client.request_embeddings` instead of one request per chunk;
  `orchestration-mcp`'s single-text query embed is unchanged. **Validated against a live
  environment**: against a real Ollama in the compose stack, a document's chunks came back
  as distinct vectors across multiple batches in input order, and an over-context chunk
  raised the worker's permanent-failure path (via `truncate: false`) instead of silently
  storing a truncated vector. **Tested against mocks only**: the 404-fallback path for an
  older Ollama predating `/api/embed` (`respx`-mocked, not exercised against a genuinely old
  pinned image) and the OpenAI-compatible batch path's out-of-order `index` handling. Not
  measured: throughput against a GPU-backed or remote endpoint, the case the batching
  argument actually rests on — see PR #410 for the CPU-only numbers showing no change there.

## Resetting

```bash
docker compose down -v   # also wipes Postgres/Qdrant/Ollama/reranker-cache volumes
```
