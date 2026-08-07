# Changelog

All notable changes to the nexus-rag stack. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org) with pre-1.0 semantics (minor releases may break).
The whole stack versions in lockstep — one version covers all five service
packages, the four published images, and the Helm chart. See
`docs/releasing.md` for how a release is cut and how this file is maintained.

Entries are curated by hand, not generated: a PR that changes behavior adds a
line under **Unreleased** in the same change (encouraged, not CI-enforced);
the release PR renames that section to the version and dates it. Write entries
for the operator or curator reading them, not for the committer — say what
changed in the running system, with the issue/PR reference for the trail.

## [Unreleased]

### Added

- `security.yml`'s `helm` job now renders a **matrix of every value combination
  the chart documents** (`vectorBackend: milvus`, each `external` block, bundled
  object store, external reranker, `serviceMonitor`, `ingress`) and
  schema-validates each rendered manifest with `kubeconform -strict` against the
  real Kubernetes JSON schemas. Previously CI proved only the default values plus
  one override, and `helm/nexus-rag/README.md` said so outright -- "render locally
  with the combination you're about to deploy before trusting it". `helm lint`
  checks chart grammar, not API validity: it passes a misspelled field or a wrong
  `apiVersion`, which then fails at `kubectl apply` time in a cluster. Two
  regression guards land alongside it: the `nexus-rag` chart must refuse to
  render without an OIDC redirect URI (mirroring the observability chart's
  existing source-ranges guard), and `vectorBackend: milvus` must not render
  Qdrant resources -- #401's fix had nothing asserting it, so a refactor could
  quietly reintroduce provisioning both vector stores, which in a classified
  deployment means chunk payloads in a second store nobody accounted for.
  kubeconform is version-pinned and SHA-256 verified, the same discipline NFR-16
  applies to images. Rendering is not installing: nothing here applies manifests
  to a live cluster, so admission control, PVC provisioning, image pulls and
  readiness remain unexercised in CI, and the chart README says so.

## [0.6.0] - 2026-08-07

### Added

- **Periodic re-verification of object-store originals against their stored
  content digest** (#432, NFR-18 follow-on to #285), independent of any
  upload/re-embed event: `app/integrity_sweep.py` (`python -m
  app.integrity_sweep`) re-hashes a bounded rolling window of documents each
  run, oldest-checked-first, and flags a digest mismatch or unreadable
  original as a `document.integrity_check_failed` audit_log entry plus a
  `nexus_rag_integrity_check_failures_total` Pushgateway metric — never an
  automatic status change, since the cause (bit rot, backup-restore
  corruption, or real tampering) needs curator/admin triage. Scheduled
  nightly by default via a new chart CronJob
  (`ingestionWorker.integritySweep`), and alerted on
  (`NexusRagIntegrityCheckFailureDetected`/`...Stale`) the same way
  reconnaissance-query detection already is.

- The golden set gains two **adversarial phrasings** and its first
  **multi-document expectation**, completing issue #528's checklist. The
  adversarial cases apply maximum lexical pressure toward a document FR-26 must
  withhold — one quotes the superseded `network-access-sop-v1.md` verbatim, the
  other borrows the rejected VPN guide's vocabulary — so a failure there is an
  FR-26 status-filter regression, not a ranking one. The multi-document case is
  the first whose `expect` names two documents, so `recall_at_k`/`precision_at_k`
  stop being 0/1-valued and a partial regression (one of two expected documents
  dropping out) reads as 0.5 instead of hiding behind a still-passing 1.0.
- A cross-configuration baseline comparison in the golden-query harness now
  **fails closed** instead of merely being annotated (#525). #514 added the
  config fingerprint (embedding/reranker model, rerank floor, content-type
  boosts, chunking, golden-set hash, persona) and flagged a mismatch, but the
  comparison still returned a verdict -- so a nightly could go red for a model
  swap and read as a quality regression, or go green having compared two
  different systems. `scripts/evaluate_retrieval.py` now exits non-zero on a
  fingerprint mismatch with a message naming the cause, and
  `--allow-config-change` permits the comparison anyway (a cross-config diff is
  sometimes the actual question) while stamping `config_change_allowed: true`
  into the comparison, so a report cannot later be mistaken for a same-config
  one. A baseline predating fingerprints still compares as unknown-config --
  warned about, not refused -- so trend history already on disk stays usable.

- The golden-query harness (`scripts/evaluate_retrieval.py`) reports advisory
  rank-aware metrics — MRR, nDCG@K, precision@{1,3,5} — alongside the gated
  recall/precision, and stamps every persisted report with a config fingerprint
  (embedding/reranker model, rerank floor, content-type boosts, chunking,
  golden-set hash, persona); baseline comparisons across differing fingerprints
  are annotated rather than silently blended (#514). `docker-compose.yml` wires
  `RERANKER_MODEL` through `reranker-service` and mirrors the fingerprint knobs
  into `eval-retrieval`, so a model swap shows up in the report instead of
  reading as quality drift.
- The golden set grows from 8 to 15 cases (#514): typo and vague multi-part
  queries, an off-topic no-relevant-document query feeding a new advisory
  `mean_abstention_noise` metric (what retrieval returns when it should return
  nothing), and `scripts/golden_queries_personas.json` — per-case querying
  personas (merged in via `--persona-set`) so recall and the FR-26 leak check
  run under bob-query/carol-curator claims sets, where the access-scope filter
  leg is the only thing excluding the Signal-Corps-scoped SECRET document.
- `scripts/benchmark_latency.py` + a `benchmark-latency` compose service
  (eval profile) measure NFR-4 retrieval latency (#514): exact client-side
  end-to-end p50/p95 at configurable concurrency, per-stage
  (embed/retrieve/rerank) estimates from the operator-side Prometheus
  histograms — response bodies still carry no timings (the issue-127 side
  channel) — plus a self-check flagging arithmetically impossible percentile
  estimates (#536). Runs advisory in the e2e golden-query job, uploading a
  `latency-benchmark-report` artifact.

- `docs/nist-ai-rmf/` — a NIST AI RMF 1.0 compliance documentation set (governance
  policy, risk register, impact assessment, system/vendor inventory, RMF outcome
  mapping, evidence index), deliberately separate from and referencing the
  engineering docs rather than duplicating them (#530). Several sections are
  marked `TBD (organizational)` pending owner decisions tracked in issues
  #519–#524.

- **Scheduled runs for the offline audit-reporting jobs** (#527), closing the
  gap where `detect_query_anomalies.py` and `calibrate_tagging_advisory.py`
  were "run on demand or on a schedule" with nothing in the repo actually
  scheduling either. The dev stack gains a `scheduling` compose profile
  (hourly detection, weekly calibration, interval-overridable); the Helm
  chart gains default-off CronJobs backed by a `scripts` image now built,
  published, and SBOM'd per release in lockstep with the four service images
  (the version-consistency check grows from 11 to 12 lockstep fields).
  Calibration also gains a content-free Pushgateway exposition and a
  `NexusRagTaggingCalibrationStale` alert mirroring the anomaly detector's
  existing heartbeat pattern.

- A Grafana panel graphing `nexus_rag_below_relevance_floor_total` — raw
  drop rate plus drops-per-query — in the retrieval dashboard's reranker
  section (#438), giving the `RERANK_SCORE_FLOOR` calibration a candidate-
  level visibility signal after a reranker model change.

- NFR-4's retrieval+rerank latency budget is now an agreed target instead of
  a guess (issue #430): `NexusRagHighQueryLatency`/`NexusRagHighQueryLatencyCritical`
  alert on p95 >1s (warning) / >2.5s (critical) against
  `nexus_rag_query_stage_seconds{stage="total"}` (embed/retrieve/rerank —
  `orchestration-mcp`'s own span), replacing the previous provisional 5s
  threshold. Thresholds are derived from a measured baseline (p95 ~250ms on
  a fresh, CPU-only dev-stack run against the seeded 7-doc corpus,
  2026-08-07), not an arbitrary number. Both `infra/observability` and
  `helm/observability` rule copies updated in lockstep. The *full*
  end-to-end budget (retrieval + rerank + generation) remains open —
  generation happens in Ollama/LiteLLM, outside this repo's instrumented
  span — and is tracked separately as issue #573.

### Security

- **Multi-stage Dockerfile build for `reranker-service`** (#553, split off
  #511/#554 for its `TORCH_INDEX_URL` build-arg complexity): a `builder`
  stage does the pip installs (including the CPU/CUDA torch wheel), the
  runtime stage copies in only the resulting `site-packages` + app source —
  same treatment #511/#554 already gave `ingestion-api`,
  `ingestion-worker`, and `orchestration-mcp` — so pip/setuptools/wheel and
  everything vendored inside them (e.g. `pip/_vendor/msgpack`) never land in
  the shipped image at all. A CUDA torch wheel's runtime shared libraries
  (bundled in the wheel itself or pulled in as separate `nvidia-*` pip
  packages) live in `site-packages` too, so they carry over with the same
  plain `COPY --from=builder`. Validated against the CPU default
  (`docker compose up --build`, trivy rescan, `eval-retrieval` smoke test);
  the CUDA path is implemented but unvalidated — no GPU in the dev stack or
  CI — per this repo's honest-confidence-labeling convention.

- `h2` bumped `4.4.0` → `4.4.1` (CVE-2026-71554, MEDIUM: duplicate `Host`
  header could facilitate request smuggling) across the four lockfiles that
  pin it (`services/common`, `services/ingestion-api`,
  `services/ingestion-worker`, `services/orchestration-mcp`;
  `reranker-service` doesn't depend on it). Surfaced issue #555: the
  `trivy-fs` CI gate (`security.yml`) was failing on this newly-disclosed
  CVE despite its `severity: HIGH,CRITICAL` filter, because
  `aquasecurity/trivy-action`'s entrypoint silently drops the `severity`
  input (`TRIVY_SEVERITY`) whenever SARIF output is requested, unless a
  second input, `limit-severities-for-sarif`, is also set — so the gate was
  actually exit-coding on every severity in the repo's dependency tree, not
  just HIGH/CRITICAL. Added `limit-severities-for-sarif: true` to the
  `trivy-fs` step to close that gap; without it, any future LOW/MEDIUM
  finding would have red the gate the same way.

- `scripts/adversarial_injection_probe.py` gained two fixtures (issue #494) —
  `travel-reimbursement-policy.md` (delimiter forgery, #458) and
  `parking-permit-guide.md` (citation hijack, #457) — and live-validated both
  fixes against real `qwen2.5:3b-instruct` generation through a full
  `alice-ingest`/`carol-curator` → LibreChat Agent → per-user MCP OAuth →
  `rag_search` round trip: neither injected canary reached the model's final
  answer. See `REQUIREMENTS.md` Section 11's #494 entry for the full
  writeup, including a live-environment tool-calling reliability issue
  (issue #540) found along the way, and a re-confirmation that the
  DAN/roleplay-reframing gap tracked by #427 remains open.

- `infra/nginx/librechat-tls.conf` (the dev TLS-terminating proxy in front of
  LibreChat, issue #75) hardened against three semgrep findings from scan
  `static-20260805T210006Z`: the plain-HTTP-to-HTTPS redirect and the
  forwarded `Host` header now use nginx's own `$server_name` instead of the
  attacker-controlled `$host`/`Upgrade`/`$http_host` (#472); the
  `Upgrade`/`Connection` headers are now whitelisted to a literal
  `"websocket"` value via two `map` blocks, so a non-websocket `Upgrade`
  value (e.g. `h2c`) gets `Connection: close` and an empty `Upgrade` instead
  of being forwarded verbatim, closing off H2C smuggling through this proxy
  (#471). `missing-internal` (#470) was triaged as a false positive and
  suppressed with a `nosemgrep` + rationale comment: that rule is for
  locations only reachable via an internal redirect, but `location /` here
  is this proxy's sole public entrypoint — adding `internal;` would 404
  every real client request. Verified live: `nginx -t` passes, a full
  `docker compose up` of `librechat-proxy` + its dependencies serves
  `/login` correctly (200), a plain-HTTP request still 301-redirects to
  `https://localhost:3080` even with a spoofed `Host: evil.example.com`
  header, and a rescan of the file with the specific semgrep rules shows
  zero findings.
- All five service/one-shot images (`ingestion-api`, `ingestion-worker`,
  `orchestration-mcp`, `reranker-service`, `scripts/Dockerfile`) now build
  `FROM python:3.13-slim` instead of `python:3.11-slim` (#455, grype
  CVE-2026-7210 + 111 related CVEs — insufficient Expat hash-flooding
  entropy). Verified live: `python:3.11-slim` and `python:3.12-slim` both
  still ship `expat_2.7.4` (vulnerable) as of 2026-08-06; only
  `python:3.13-slim` (3.13.14) ships the fixed `expat_2.8.1` — CPython
  hasn't backported the Expat fix to the 3.11/3.12 branches, so a patch-level
  rebuild alone doesn't fix this. All five hash-pinned `requirements.txt`
  lockfiles were regenerated under Python 3.13 (`pip-compile
  --generate-hashes`, `--allow-unsafe` for reranker-service as before); every
  resolved package version is unchanged from the 3.11 lockfiles except
  conditional-dependency comment noise (Python-version-gated deps like
  `anyio`/`starlette`/`psycopg` no longer pull in `typing-extensions` under
  3.13). Verified live end-to-end: all four service images build clean, all
  gated per-service pytest suites plus the repo-root `tests/unit`/`tests/e2e`
  suite pass unchanged, `ruff`/mypy/NFR-16 pin-check all pass, and a full
  `docker compose up` + golden-query eval (`--profile eval run
  eval-retrieval`) against the rebuilt stack shows recall@K 1.0, zero
  forbidden-document leaks. Also fixed an unrelated, pre-existing mypy break
  in `reranker-service/app/main.py` (`CrossEncoder.predict()`'s stub
  widened to a multimodal union in sentence-transformers 5.6.1, reproduced
  identically under real Python 3.11 too, so unrelated to this bump) with a
  scoped `# type: ignore[arg-type]` — otherwise it silently blocked the
  `types` CI job regardless of this change; tracked structurally in #517.
- `services/common/common/qdrant_store.py`'s classification-migration
  failure log now escapes `document_id`/`new_name`/`current_collection`
  through `log_safety.log_safe` before interpolation (#465, CodeQL
  `py/log-injection`), matching the pattern `purge.py`'s equivalent log
  line already used. `purge.py`'s three flagged lines were already fixed
  (predates this scan) — only this one call site needed the change.
- `scripts/adversarial_injection_probe.py`'s four LibreChat HTTP calls now
  verify TLS against the project's own dev CA (`infra/certs/ca.crt`)
  instead of disabling verification outright (#453, CodeQL
  `py/request-without-cert-validation`). `infra/certs/generate-dev-certs.sh`
  now stamps the CA cert with a `keyUsage` extension — required for
  Python's `ssl` module to accept it as a verification root at all, even
  though `openssl verify` accepted the old cert without it. Verified live:
  regenerated dev certs locally, confirmed `openssl verify` chain-validates
  and an `httpx` client with `verify=infra/certs/ca.crt` successfully
  trusts a server presenting the regenerated `dev.crt`/`dev.key` (via
  `openssl s_server`) — the full script itself needs a live LibreChat run
  to exercise end-to-end, which this did not do (not a CI gate, per its
  own module docstring). `infra/certs/*` is gitignored and regenerated
  per-machine, so existing local certs need `rm infra/certs/{ca,dev}.*`
  + a re-run of the generate script to pick this up.
- `security/triage.py:874`'s reported `chmod` world-readable finding
  (#452) does not correspond to any file in this repo, in the working
  tree or anywhere in git history — closed as a stale/non-reproducing
  scan artifact rather than fixed, same class as #454/#456/#462/#463.
- All 4 service Dockerfiles now uninstall `pip`, `setuptools`, and `wheel`
  as a final root-level step, after all application dependencies are
  installed (#491). Fixes CVE-2025-47273 (setuptools) and
  GHSA-6v7p-g79w-8964 (msgpack) — both live only in `pip`'s own
  internally-vendored copies (`pip/_vendor/`), separate from the
  app-facing `setuptools` upgraded by #450/#451's fix, and unreachable
  through the hash-pinned application lockfiles. None of the 4 services
  invoke `pip` at runtime, so removing it (and everything vendored
  inside it) outright closes this class of finding rather than chasing
  individual vendored CVEs release over release. Verified live: before/
  after trivy rescans of all 4 rebuilt images (HIGH/CRITICAL clean after),
  plus a full `docker compose up` + `--profile eval run eval-retrieval`
  smoke test (all services healthy, golden-query recall 1.0, zero
  forbidden-document leaks). This is an interim fix — #511 tracks
  splitting each Dockerfile into a multi-stage build so `pip`/
  `setuptools`/`wheel` never enter the runtime image's layers at all,
  which structurally prevents future CVEs in whatever `pip` vendors next
  from resurfacing this same finding.

### Fixed

- Seeded dev realm's `eve-purge` login failing with `invalid_grant` was a
  stale-volume artifact, not a defect in the realm export (#480, found by
  manual review): `eve-purge` was added later than the other four seeded
  users (#298), and Keycloak's `--import-realm` only imports into a fresh
  `keycloak_db` — a Keycloak Postgres volume created before #298 keeps its
  existing realm and never picks up the new user. Verified live: a fresh
  `docker compose down -v` + `up` imports all five seeded users correctly,
  `eve-purge` included, confirming this was never exercisable on a stale
  volume rather than broken outright. `docs/dev-setup.md`'s seeded-users
  section now documents the re-import step, mirroring the existing #229
  schema note's pattern for the same "fresh volume required" class of gap.

### Security

- Added a single-curator `suspend` transition
  (`POST /curate/{id}/suspend`, `services/ingestion-api/app/routes/curate.py`)
  for an already-`approved` document (#478, found by manual review).
  Previously the only way to stop serving a wrongly-classified or
  wrongly-releasable approved document was `reject()` (409s once a document
  has left `pending_review`) or the two-person purge flow (#279 gap G3) —
  the right gate for *destruction*, but a strange prerequisite for simply
  taking something out of circulation while its tags get sorted out.
  `suspend` demotes the document back to `pending_review` — the same
  reversible target `edit_metadata`'s #268 authority-mismatch demotion
  already uses, and already excluded by the FR-26 retrieval filter — so
  nothing is destroyed and the document lands back in the ordinary
  curation queue for re-approval, correction, or rejection. Available to
  any curator with existing authority over the document (no new role); the
  curation "List" dashboard gained a matching "Suspend" button.
- `scripts/Dockerfile` — the image every one-shot Compose container shares
  (`seed-sample-data`, `eval-retrieval`, `calibrate-tagging-advisory`,
  `detect-query-anomalies`, `ingest-classification-corpus`,
  `verify-corpus-access`) — now runs as a fixed non-root UID/GID (10001),
  matching the convention the four service Dockerfiles already use (#459,
  #464). It had no `USER` at all, so every one of those containers ran as
  root. Verified live: rebuilt the image and ran all six one-shots against a
  real `docker compose up` stack with no permission errors — none of them
  write anywhere but `/srv`'s own bytecode cache at runtime. This is a
  narrower fix than the four services' full hardening treatment (no
  `read_only`/`cap_drop`/Compose-level `user:` override, since
  `scripts/Dockerfile`-built services are intentionally outside
  `check_compose_hardening.py`'s `CUSTOM_SERVICES` set) — see #502 for a
  follow-up on the `ONE_SHOT` `no-new-privileges` exemption instead.
- Triaged two MEDIUM semgrep findings from local scan `static-20260805T210006Z`
  as false positives, with documented `nosemgrep` suppressions rather than
  code changes (#468, #469). `base.html:129`'s anchor `href` only ever
  renders one of two hardcoded literal strings (`/curate` or `/curate/list`),
  chosen by a server-derived boolean from verified OIDC claims — there's no
  variable content and no path to the flagged `javascript:` URI injection.
  `upload.html:360`/`362`'s inline `<script>` values (`no_releasability_
  restriction`, a fixed sentinel from `common/metadata.py`; `max_batch_files`,
  an admin-set env var) are never user input, and both pass through Jinja2's
  `| tojson` filter — the JS-context-safe encoder the rule's own remediation
  asks for. Verified live against a real `semgrep scan --config
  p/security-audit --config p/python` run before and after, plus the full
  `services/ingestion-api` test suite (including `test_csp_templates.py` and
  `test_template_xss.py`).
- All 4 service Dockerfiles now run `apt-get upgrade` and `pip install
  --upgrade pip setuptools wheel` immediately after `FROM`, before any
  application dependency is installed (#450, #451). Fixes CVE-2026-23949 and
  CVE-2026-24049 (HIGH) — `jaraco.context`/`wheel` copies vendored inside an
  outdated `setuptools`, sitting underneath the hash-pinned application
  lockfile and unreachable through it. `apt-get upgrade` is defense-in-depth
  against OS package drift between the base image's own rebuilds; a live
  rescan found the current `python:3.11-slim` (debian trixie) layer already
  clean of fixable HIGH/CRITICAL OS CVEs. See #491 for a follow-up: `pip`
  itself vendors an older `setuptools`/`msgpack` for its own internal use,
  which a rescan after this fix still flags — narrower risk (pip's CLI is
  never invoked at runtime in these images) but not yet resolved.
  #454, #456, #462, #463 are the same two CVEs re-filed per-tool
  (trivy/grype) from the same scan run that produced #450/#451 — already
  fixed by this same change, confirmed by re-checking installed versions
  inside the built images (`wheel` 0.47.0, vendored `jaraco_context`
  6.1.0-dist-info), closed as duplicates rather than tracked separately.
- `orchestration-mcp`'s `/debug/rag_search` REST route now defaults to
  **disabled** (#476), matching #214's originally stated intent -- the code
  had shipped defaulting to enabled ever since, and the Helm chart never set
  the env var at all, so every Helm-deployed environment served the route
  regardless. Authorization was (and is) enforced on it either way, so this
  closes unnecessary surface, not an auth bypass -- notably the URL
  query-string fallback (`?query=`), which lands the query text in proxy/
  ingress logs, the exact exposure #125 removed from the audit log for the
  same reason. This route isn't only a curl-shaped dev convenience, though:
  `ingestion-api`'s `/search` page (`app/routes/search.py`) proxies to it
  unconditionally with no enablement of its own, so both `docker-compose.yml`
  and the Helm chart (`orchestrationMcp.debugEndpointEnabled`, new value,
  default `true`) now opt back in explicitly by default -- only an
  unset/blank env var lands on the closed default. Found while investigating
  #476: the `/search` proxy itself always sends the query via URL params
  rather than a JSON body, independently reintroducing the same log exposure
  on every real query -- filed separately as #496, not fixed here.
- `ingestion-api`'s `/search` page (`app/routes/search.py`, proxies to
  `orchestration-mcp`'s `/debug/rag_search`) sent the query as a URL query
  parameter on every call instead of a JSON body (#496, found while
  investigating #476). `/debug/rag_search` supports the JSON-body form
  specifically so a question asked of a classified corpus doesn't end up in
  proxy/ingress access logs (#125/#214) — this route is the production path
  behind every `/search` page query, so it hit that exposure on every real
  query, not just the deprecated `?query=` fallback. Now sends `json=`
  instead of `params=`; the caller's bearer token forwarding is unchanged.
- Triaged two HIGH semgrep findings from local scan `static-20260805T210006Z`
  as false positives, with documented `nosemgrep` suppressions rather than
  code changes (#460, #461). `claims.py:179`'s `jwt.decode(verify_signature=
  False)` only runs inside the `OIDC_SKIP_VERIFY` dev-only escape hatch
  (default off, CRITICAL-logged at import per #215, documented as
  never-set-in-prod) — the real verification path a few lines below always
  checks signature/audience/issuer. `db.py`'s `text(f"ALTER TABLE ...")` in
  `_ensure_columns` (now split out to `_add_column`) only ever interpolates
  table/column names from the hardcoded `_ADDITIVE_COLUMNS` dict, never user
  input — and the suggested `or_()`/`and_()` remediation doesn't apply
  regardless, since this is DDL, not a query, and SQL identifiers can't be
  bind-parameterized the way values can. Verified live against a real
  `semgrep scan --config p/security-audit --config p/python` run before and
  after.

### Added

- Content-Security-Policy on the document portal (#443, found by the same
  OWASP ZAP scan as #444/#445): `ingestion-api` now sends a per-request-nonce
  CSP (`script-src 'self' 'nonce-...'`, `object-src 'none'`, `base-uri 'self'`,
  `frame-ancestors 'none'`) on every response, the defense-in-depth layer
  behind Jinja2 autoescaping should a future template edit reintroduce an
  unescaped interpolation. Four inline `onclick="..."` attributes (logout,
  and three "Refresh" buttons) had to move to `addEventListener` as part of
  this -- a script-src nonce covers `<script>` elements, not attribute-based
  event handlers, so those would otherwise have silently stopped firing.
  Validated against a live environment: a real Chromium session driven
  through the actual Keycloak OIDC login flow visited every page and
  exercised the converted buttons with zero CSP violations and zero console
  errors.
- Static security response headers on every HTTP-facing service (#444,
  #445, found by an OWASP ZAP scan): `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer` on `ingestion-api`, `orchestration-mcp`
  and `reranker-service`, plus `X-Frame-Options: DENY` on `ingestion-api`
  specifically, closing the clickjacking gap NFR-14's CSRF protection
  doesn't cover on the curation UI's approve/reject/correct actions (see
  `ARCHITECTURE.md` §4.4). Shared as one ASGI middleware
  (`services/common/common/security_headers.py`); `reranker-service`
  carries its own small inline duplicate rather than taking a new
  dependency on `services/common`, consistent with how it already
  duplicates its tracing/profiling setup.
- A containerized integration test layer (#428): `tests/integration/`, run
  against a live Postgres by a new opt-in `e2e.yml` job (`integration`, same
  `needs-e2e`-label gating as the golden-query/browser-verify jobs). First
  suite: `test_nfr2_audit_log_append_only.py`, which connects directly as
  each application database role and the dedicated audit-reporting role and
  asserts NFR-2's append-only enforcement — `INSERT`/`SELECT`/`UPDATE`/
  `DELETE` succeed or fail exactly as `infra/postgres/grant-matrix.sql`
  says they should — against a real Postgres, closing a gap in-memory
  SQLite mocks structurally can't cover. `docker-compose.ci-integration.yml`
  overlays a host-reachable port onto Postgres for this job/local
  reproduction only; the base compose file's Postgres service still has
  none. Validated against a live environment, including a deliberate
  negative control (manually re-granting `SELECT` to the ingestion-api role
  turns the corresponding test red; revoking it turns the suite back
  green). Scoped deliberately to Postgres/NFR-2 only for this change —
  extending the layer to Qdrant/NATS (NFR-11 crash-redelivery, NFR-13 live
  revert-on-partial-failure, which need a fault-injection design decision
  first) and Keycloak (gating `ingestion-api` route tests and
  `orchestration-mcp/app/rag_search.py`'s coverage) are tracked separately
  as #439 and #440.
- Detection of reconnaissance-shaped retrieval patterns (#426, closing #127
  gap #4): `scripts/detect_query_anomalies.py` mines the FR-31 audit log for
  four per-identity signals over a lookback window — attempt-rate spikes,
  a sustained personal denial ratio, a high share of queries resolving to
  0-1 chunks (narrow, membership-inference-shaped probing), and repeated
  denial-then-success sequences (an identity's `rag-query` grant changing
  state mid-window and being used immediately after). Run on demand or
  scheduled via `docker compose --profile anomaly-detection run --rm
  detect-query-anomalies`; reporting only, reusing the existing
  `nexus_rag_audit_reporting` role (no new grant). Publishes a content-free,
  bounded per-signal count to Prometheus via Pushgateway — never a
  per-identity label — behind two new alert rules,
  `NexusRagQueryAnomalyDetected` and `NexusRagQueryAnomalyDetectionStale`.
  Validated against a live environment: real audit rows generated by
  `bob-query`/`alice-ingest` calls correctly flagged `high_volume`/
  `narrow_probe_shaped`/`high_denial_ratio`, and the resulting Pushgateway
  metrics correctly fired `NexusRagQueryAnomalyDetected` in a real
  Prometheus.
- `ingestion-worker`'s image-captioning, LLM classification-suggestion, and
  LLM PII-advisory calls (`VISION_MODEL`/`CLASSIFICATION_MODEL`/`PII_LLM_MODEL`)
  can now target an OpenAI-API-compliant hosted model (vLLM, TGI, a cloud
  chat-completion endpoint) instead of only Ollama's native `/api/generate`
  (#418, Phase 1 of the ask split from #403's Note; reranking is tracked
  separately as #419). Shares `embeddingService.external.apiCompatibility`/
  `.apiKey` with the embedding client (#403) — one config knob, since all
  four features point at the same instance — through a new
  `common/completion_client.py`. Vision prompts carry the image as an
  `image_url`/base64-data-URI content part in the OpenAI-compatible case.
- `orchestration-mcp`'s reranking call can now target a genuinely external
  reranker instead of only this chart's own `reranker-service` (#419, the
  decision split from #418): `rerankerService.enabled: false` +
  `rerankerService.external.host`, same enabled/external pattern as
  `embeddingService`. `apiCompatibility` selects `"internal"` (default,
  unchanged), `"tei"` (HuggingFace text-embeddings-inference's native
  `/rerank`, the recommended default for a real external endpoint), or
  `"cohere"` (the Jina/Cohere-style `/v1/rerank` convention — also what
  vLLM's own rerank endpoints speak; vLLM does **not** speak the `"tei"`
  shape despite hosting cross-encoder rerankers too, so `"cohere"` is the
  mode to use against vLLM). Authenticates with a bearer token from
  `rerankerService.external.apiKey` in `"tei"`/`"cohere"` mode.
- Optional retrieval relevance floor (#394): set `RERANK_SCORE_FLOOR`
  (Compose) / `orchestrationMcp.rerankScoreFloor` (chart) to the minimum
  post-rerank cross-encoder score a chunk needs to be returned. A query where
  every candidate falls below it returns the explicit "no approved document
  covering the question was found" message — counted as an `empty` query
  outcome and audit-logged with the reason — instead of its least-bad `top_k`.
  Unset (the default) keeps today's behavior; `-5.0` is the measured
  permissive starting point on the dev corpus, and the value should be
  re-tuned after any reranker model change. Per-candidate drops are visible
  as `nexus_rag_below_relevance_floor_total`. The floor is applied to the
  score the ranking actually sorted on, so it composes with `#419`'s external
  reranker modes — re-tune it against whatever model that endpoint serves.

- `docs/observability.md` now documents how to run the Q→C→A evaluation on a
  schedule instead of by hand (#388): the CronJob shape that publishes to the
  in-cluster Pushgateway with no `kubectl port-forward`, the credentials such a
  run needs, and the two settings that quietly defeat it (a fail-closed
  evaluator skipping its own publish step on the regression run that matters
  most, and a non-persistent history directory leaving every run baseline-less).
  Documentation only — the chart deliberately ships no such template yet,
  because `evaluate_rag_quality.py` cannot run unattended until its `localhost`
  endpoint literals and credential path are addressed; the section enumerates
  exactly what those are.

### Security

- New operator runbook, `docs/siem-detection-runbook.md` (#436), for building
  the four reconnaissance-shaped query detections (#426) inside the
  deployment's own SIEM rather than only via periodic
  `scripts/detect_query_anomalies.py` runs. Documents the RFC 5424 message
  shape a rule has to parse (#73's export: JSON in the syslog MSG field,
  action as MSGID, WARNING severity for `*.denied`), each signal's threshold
  and gating minimum, and adaptable Splunk SPL / Elastic ES|QL and EQL
  sketches. Documentation only — no code changed, and the queries ship as
  sketches to adapt rather than tested artifacts, because SIEM query
  languages stay outside this repo's testable surface (the reason
  `docs/threat-model.md` section 4 recorded this as a residual in the first
  place). Two corrections a reader needs are called out explicitly: there is
  no query text to correlate on (#125 never stored it, which is why
  `narrow_probe_shaped` keys on `result_count`), and `boundary_mapping`
  does not detect access-filter-edge probing despite its name — it detects a
  `rag-query` grant changing state mid-window, since an out-of-scope query
  returns a successful empty result rather than a denial.

- The FR-30/FR-32 retrieval-quality regression gate now actually runs unattended
  (#429). `e2e.yml`'s `golden-query` job restores the previous nightly's trend
  store from a workflow artifact, passes `--history-dir` so
  `scripts/evaluate_retrieval.py` auto-baselines against it, and republishes the
  store -- the cross-run persistence #71 left open, without which the harness's
  `--history-dir`/`--baseline` support had no prior report to compare against in
  CI and only a forbidden-leak (FR-26) failure could ever turn a nightly red.
  Every nightly becomes the next baseline: that catches a step change (a
  chunking, embedding-model or reranker change that drops recall overnight) and
  deliberately does not catch slow cumulative drift, so a release-pinned
  baseline is the remaining half and is recorded as such in `docs/testing.md`.
  PR runs never write to the store, so a feature branch's numbers cannot become
  the baseline; their behaviour is otherwise unchanged. The store is
  republished even on failure, so the next run compares against the most recent
  run rather than the last green one.

- Strengthened `orchestration-mcp`'s retrieved-content `SECURITY_NOTICE` to
  name persona/roleplay/compliance-marker reframing explicitly, targeting
  the residual gap issue #97's live evaluation found (a DAN-style injection
  got a partial compliance out of both dev-default generation models even
  though the notice was in place). More significant: found and fixed that
  `format_rag_search_for_model` — the function that builds the text the
  real `rag_search` MCP tool actually returns to a calling model — had
  never included `SECURITY_NOTICE` at all; only a short, independently
  worded line ever reached LibreChat's agent, so #97's finding was measured
  against a materially weaker notice than the `/debug/rag_search` JSON
  response carries. Both surfaces now carry identical wording (#427).
  Also: `common/content_advisory.py`'s curator-facing advisory scan
  (issue #284) gained DAN/roleplay-marker phrases, giving a curator
  visibility into this injection style at approval time. Implemented and
  the tool-call wiring fix was directly exercised against a running
  `orchestration-mcp`; a live re-run of `scripts/adversarial_injection_probe.py`
  confirming the strengthened wording changes real generation-model
  behavior was attempted but not completed cleanly this session (unrelated
  dev-environment issues — see REQUIREMENTS.md Section 11) and is still
  needed.
- Closed a delimiter-forgery gap in `orchestration-mcp`'s retrieved-content
  wrapping (#458, found by a local rag-poisoning scan, regression/residual
  of #427): a document's own text containing a literal
  `</untrusted_document_content>` closed the real untrusted-content boundary
  early, and a forged reopening right after it made injected content read,
  to the model, as sitting outside the untrusted region — indistinguishable
  from this module's own trusted framing. `_delimit_untrusted_text` now
  neutralizes any literal marker occurrence in the source chunk text before
  wrapping it, so at most one real open/close pair can exist per delimited
  passage regardless of document content. `common/content_advisory.py`'s
  curator-facing advisory scan also gained the literal marker strings as
  injection-marker phrases. Separately (#457, same scan): `SECURITY_NOTICE`
  gained an explicit warning against copying a passage's wording verbatim
  as if it were the model's own answer — a poisoned document worded as a
  complete, ready-to-copy answer (with a foreign token riding along) had
  been echoed back verbatim by the model. The delimiter-forgery fix is
  deterministic and fully covered by unit tests
  (`services/orchestration-mcp/tests/test_tool_response.py`,
  `tests/unit/common/test_content_advisory.py`); like #427's wording change,
  the citation-hijack wording has not yet been validated against real
  generation models — tracked in #494.

### Fixed

- `scripts/adversarial_injection_probe.py`'s `_force_clear_mcp_tokens`
  matched stored MCP OAuth token identifiers against `^mcp:{server}:` (a
  trailing colon), but LibreChat actually stores the bare `mcp:{server}`
  with no trailing colon or suffix — so a stale token was never actually
  cleared before a reconnect attempt, silently defeating the "force clear"
  the function exists to do (#427, found live while re-running the probe).
- `scripts/seed_sample_data.py` is now idempotent: a re-run resumes or skips
  each of its 7 sample documents based on prior-run state (`/documents/mine`)
  instead of unconditionally resubmitting them (#411, #413). Previously every
  `docker compose --profile eval run --rm eval-retrieval` re-triggered seeding
  via its `depends_on`, silently growing the dev corpus by 7 documents per
  invocation and drifting local eval metrics with how many times they'd been
  run; CI was unaffected (fresh volumes every run).

### Security

- A Qdrant volume that predates the #229 per-classification collection split
  can still hold chunk text in the bare `nexus_rag_chunks` collection (#477,
  found by manual review). Nothing ever queried it after #229 (correct —
  `existing_classification_collections` only matches the `<name>__` prefix)
  but nothing ever purged it either, so `purge_document()` could report
  success while that collection's copy of a "destroyed" document's chunk
  text survived indefinitely — a retention/lineage gap, not a retrieval
  bug. `delete_document_chunks` (`services/common/common/qdrant_store.py`,
  called by both purge and supersession) now sweeps the bare
  `QDRANT_COLLECTION` name too, alongside every classification collection,
  if it still exists — narrowly scoped to the destruction path rather than
  folded into `existing_classification_collections` itself, since that
  helper also drives #122's embedding-model provenance check, which must
  not sample stale pre-migration data.

### Changed

- `RERANK_SCORE_FLOOR` (#394) now has a validated calibration behind its
  default instead of an unvalidated starting-point guess (#431).
  `scripts/calibrate_rerank_floor.py` reproducibly measures, against
  `scripts/golden_queries.json` on a freshly seeded dev corpus, the interval
  any floor value must sit inside to preserve full recall on answerable
  queries while triggering abstention on both `expected_abstention` cases —
  measured as `(-6.141, -4.257]`. `-5.0` sits inside it and is
  live-verified end to end (`evaluate_retrieval.py`: recall@K = 1.0,
  precision@K = 1.0, 0 forbidden leaks, both abstention cases correctly
  suppressed). `.env.example` now ships `RERANK_SCORE_FLOOR=-5.0` as its
  default (previously unset); the Helm chart's `orchestrationMcp.rerankScoreFloor`
  is deliberately left unset — production corpora and reranker models differ
  from the dev corpus this was calibrated against, so turning the floor on
  in a real deployment stays an explicit operator decision, not something
  this chart flips silently on upgrade. See `docs/testing.md`'s
  "RERANK_SCORE_FLOOR calibration" section for the full methodology.

### Security

- `ingestion-api`, `ingestion-worker`, and `orchestration-mcp` Dockerfiles
  are now multi-stage builds (#511, structural follow-up to #491's interim
  fix): a `builder` stage does the pip installs (needs pip/setuptools/wheel
  present, and self-uninstalls them from its own site-packages before the
  copy below); the runtime stage only `COPY --from=builder`s the resulting
  site-packages plus app source, and separately uninstalls whatever
  pip/setuptools/wheel `python:3.13-slim` itself ships baked in, before
  that copy lands. Net effect: pip/setuptools/wheel (and everything
  vendored inside them, e.g. `pip/_vendor/msgpack`) never sit in the
  shipped image at all, rather than being installed then uninstalled every
  build — there's no longer an app-facing pip/setuptools/wheel upgrade
  cycle in the runtime stage to keep re-triggering this class of finding
  as pip's vendored tree changes release over release. Each of the 3
  images also shrinks by ~20-25MB. `reranker-service` is deliberately not
  included — split off to #553, since its `TORCH_INDEX_URL` CPU/CUDA
  build-arg selection needs the runtime stage to carry whatever CUDA
  libraries a CUDA torch wheel needs, which the other 3 images don't have
  to deal with. Verified live: before/after trivy rescans of all 3
  rebuilt images (zero pip/setuptools/wheel/msgpack findings, both before
  and after — #491 already closed those; multi-stage just removes the
  mechanism that could resurface them), plus a full `docker compose up`
  + `--profile eval run eval-retrieval` smoke test (all 3 rebuilt services
  healthy, golden-query recall/precision 1.0, zero forbidden-document
  leaks).

## [0.5.0] - 2026-08-05

### Changed

- `abstention_accuracy` is now diagnostic-only and no longer participates in
  the Q→C→A baseline comparison (#386). Measured against a fabricated,
  non-abstaining answer it scored a correct abstention 2 times in 3 on
  `qwen2.5:3b-instruct` and 3 times in 3 on `qwen2.5:7b-instruct`, so it cannot
  distinguish a real abstention failure from a lucky pass in either direction.
  It is still computed, reported, and published — a `0.0` is worth
  investigating — but it can no longer raise or suppress a regression verdict,
  and `docs/testing.md` drops the unmeasured claim that a 7B judge mitigates
  this.

### Added

- `reranker-service` now states its input window as a decision
  (`RERANKER_MAX_LENGTH`, default 512, matching the pinned model's own
  `max_position_embeddings`) instead of inheriting an unstated tokenizer
  default, and scores an oversized `(query, chunk)` pair as the max over
  overlapping windows rather than silently truncating it to the chunk's head
  (#414, closes #393). Measured on this stack's dev corpus, 12% of chunks
  overflowed the window paired with a typical query, with a mean overflow of
  323 tokens and a worst case of 67% of the chunk unseen by the model; window
  scoring lets a relevant passage in the tail count instead. Every oversized
  chunk is counted regardless of handling via
  `nexus_rag_reranker_oversized_chunks_total{handling="windowed"|"truncated"}`.
  `RERANKER_WINDOW_SCORING=false` restores the previous head-truncation
  behavior (still counted in the metric) for anyone who wants the visibility
  without the scoring change.
- The DB pool recycle interval is now configurable via `DB_POOL_RECYCLE_SECONDS`
  (`externalPostgres.poolRecycleSeconds` in the chart), overriding #236's
  hardcoded 30-minute default for environments whose Postgres sits behind an
  intermediary (PgBouncer, a cloud proxy/LB) with a shorter idle timeout; `-1`
  disables recycling (#390, closes #389). Not a correctness setting --
  `pool_pre_ping` already replaces a dead connection at checkout, so a
  mismatched value costs round trips, not errors. An unset, empty, or
  unparseable value falls back to the 1800s default with a `WARNING` log
  rather than crashing the service at startup, since the parse runs at import
  of `common.db`, which all three services import.
- `ingestion-worker` now batches chunk embedding requests through Ollama's
  `/api/embed` (or the OpenAI-compatible endpoint's native array `input`)
  instead of one `/api/embeddings` request per chunk, bounded by
  `EMBEDDING_BATCH_SIZE` (`ingestionWorker.embeddingBatchSize` in the chart,
  default 32) (#396, closes #396). Cuts a 100-chunk document from 100 round
  trips to 4, which matters most against a remote/GPU-backed endpoint where
  per-request latency, not model compute, dominates; measured no throughput
  change on the CPU-only dev stack, where per-chunk compute already
  dominates. Response vector order is asserted against input order, not
  assumed, and a count mismatch is a permanent failure rather than a
  guessed alignment. Sends `truncate: false` so an over-context chunk still
  errors loudly (matching the legacy endpoint's behavior that
  `app/chunking.py`'s size bounds were built against) instead of silently
  storing a tail-truncated vector. An older pinned Ollama without
  `/api/embed` (404) falls back to the pre-batch per-chunk behavior,
  logged once per endpoint. An unparseable or `< 1` value falls back to the
  default 32 with a `WARNING` log, same degrade-loudly discipline as
  `DB_POOL_RECYCLE_SECONDS` above.
- `rag_search` now collapses same-document, adjacent-index chunk pairs after
  reranking, keeping the better-ranked side and backfilling the freed slot
  from the rest of the already-fetched candidate pool (#395, closes #395).
  FR-4's chunk overlap means neighbouring chunks share text by construction,
  so a query matching that shared region previously could return two
  near-identical `top_k` slots instead of one; measured on an 82-document/
  1278-chunk dev corpus at 5% of `top_k=5` slots and 7.5% of `top_k=10`
  slots. Applies to both the cross-encoder and degraded (reranker
  unreachable) paths, and the response note reports how many were
  collapsed. Cross-document near-duplicates are out of scope (FR-7
  supersession already covers the intended-duplicate case).
- Embedding requests can now target an OpenAI-API-compliant hosted model
  (vLLM, TGI, a cloud embedding endpoint), not just an Ollama-compatible one
  (#403, Phase 2 of #401): `ingestion-worker`'s `embed_texts` and
  `orchestration-mcp`'s `_embed_query` both delegate to a new shared
  `common/embedding_client.py`, selected by `EMBEDDING_API_COMPATIBILITY`
  (`ollama`, default, unchanged behavior, or `openai`). Helm wiring is
  `embeddingService.external.apiCompatibility`/`.apiKey` (bearer token, same
  `existingSecret` pattern as everywhere else in the chart).
- A host-side Q→C→A quality evaluator now drives the real LibreChat Agent
  generation path and scores its ordered retrieval contexts with the local Ollama
  judge: contextual relevance/recall/precision, faithfulness, answer
  relevance/correctness, citation validity, and abstention behavior. Reports omit
  corpus text by default and baseline comparisons require the same judge, prompt,
  and golden set (#74). Abstention cases the judge will not decide are recorded
  as undetermined and counted, rather than failing the run: with the default 3B
  judge that verdict was returned as `null` even for correct abstentions, which
  made a default run report a generation regression that had not happened.
- Governance now adapts the relevant digital controls from DoDM 5200.01
  Volumes 1 and 2 and 32 CFR Part 2001 into an explicit classified-information
  profile. “Adaptive classification” is constrained to human-authorized,
  fail-closed handling; the profile separately lists the marking, authority,
  compilation, special-category, and chat-output gaps that remain before a
  deployment could claim the profile is operational.
- Q-to-C-to-A evaluation reports can now be published as sanitized Prometheus
  metrics through an opt-in Pushgateway path, and a new Grafana dashboard shows
  aggregate gauges/trends, comparable-baseline deltas, run validity, hashed
  case diagnostics, and configuration-change annotations without exporting
  query, answer, context, source, user, model, or error text (#384). The
  undetermined-abstention count travels with the abstention score, so a run
  where the judge declined some verdicts cannot read as full coverage.
- `helm/nexus-rag` can now connect to an already-running Qdrant, Milvus,
  NATS, or Ollama-compatible embedding instance instead of deploying its
  own, for each independently (#401): set `<component>.enabled: false` and
  populate the new `<component>.external.host`/`.port`/`.tls` values --
  `qdrant.apiKey`, `milvus.auth`, and `nats.credentials` still point at a
  pre-created Secret either way, same as before. The chart fails the render
  with a clear message if `enabled: false` is set without an `external.host`
  (`_helpers.tpl`'s new `nexus-rag.qdrantUrl`/`milvusUrl`/`natsUrl`/
  `embeddingUrl`), rather than silently emitting a broken URL.
- Milvus (the either/or alternative vector backend, #160) now gets the same
  default-deny ingress `NetworkPolicy` Qdrant already had, restricted to
  `ingestion-worker`/`ingestion-api`/`orchestration-mcp` plus the new
  `networkPolicy.extraMilvusClients` — previously missing entirely, despite
  holding the same cleartext chunk payload the Qdrant policy exists to
  protect (#402).
- `objectStore.enabled: true` deploys a single-node bundled SeaweedFS
  instance (`weed server -s3 -filer`) instead of requiring an external
  S3-compatible endpoint, joining Qdrant/Milvus/NATS/the embedding
  service's existing self-deploy-or-connect pattern (#407, closes #404).
  Default stays `false` — external-only was this chart's only object-store
  behavior before this issue, so an upgrade must not start provisioning a
  new `StatefulSet`/PVC without an explicit opt-in; confirmed the default
  render is byte-identical before/after. `externalObjectStore` is renamed
  to `objectStore.external` for consistency with the other components'
  shape. A same-pod bucket-init sidecar creates the bucket at startup,
  retrying the actual `s3.bucket.create` call (not just the readiness
  probe) — live minikube testing surfaced a real startup race where the
  master answers `/cluster/status` before the filer has registered with
  it, which the old readiness-only retry silently rode past. The
  `seaweedfs` `NetworkPolicy` opens the S3 port only to
  `ingestion-api`/`ingestion-worker`; master/volume/filer stay unreachable
  from the rest of the cluster.
- The golden-query harness gains dense-leg headroom (#397, closes #397):
  two new paraphrase queries share no content word with their target
  document (verified against every seeded document's text, title
  included), so only dense similarity — not BM25 — can rank the target
  into `top_k=2`, making a dense-leg regression (a broken task prefix, a
  swapped model) visible as a recall miss for the first time. The
  original five queries saturate recall@5 at 1.0 for any pipeline given
  the seeded corpus's size, so they couldn't have caught one.
  `evaluate_retrieval.py` also now actually fails the run on a recall
  miss (`recall_misses()` + `--fail-on-miss`, default on) — despite
  e2e.yml, `docs/testing.md`, `docs/threat-model.md`, and this file
  describing that behavior all along, the compose eval run passes no
  baseline and previously only exited non-zero on an FR-26 leak or a
  baseline regression, printing `[MISS]` and exiting 0 on a plain recall
  miss. Abstention queries (empty expect, recall `None`) are exempt —
  their contract is the leak check, not recall.

### Fixed

- `vectorBackend: milvus` no longer also deploys a Qdrant `StatefulSet`/
  `Service` and requires `qdrant.apiKey.existingSecret` to exist regardless
  (#401): `QDRANT_URL`/`QDRANT_API_KEY` and the Qdrant chart resources now
  render only when `vectorBackend` is actually `"qdrant"`, matching
  `MILVUS_URL`'s existing either/or behavior. Previously any deployment that
  set `vectorBackend: milvus` without also manually setting
  `qdrant.enabled: false` got both backends provisioned at once, and one
  that did set it false failed to start on a missing Qdrant secret it never
  needed.
- Dense embedding requests now carry nomic-embed-text's required
  `search_document: `/`search_query: ` task-instruction prefixes (#392):
  ingestion previously embedded chunks and orchestration-mcp previously
  embedded queries with neither prefix, which doesn't error but does mean
  dense retrieval was running the model outside its trained (asymmetric)
  regime. Prefixes are looked up per-model (`common/embedding_prefixes.py`)
  so a differently-configured `EMBEDDING_MODEL` isn't guessed at. Folded into
  the #122 stamped embedding identity, so a corpus embedded before this fix
  is refused by the mismatch check rather than silently compared against
  newly-prefixed queries -- re-embed it with `python -m app.reembed`.
- `scripts/export_release_bundle.sh` verifies each saved image tar is
  actually complete instead of just present (#399, closes #399): on a
  Docker daemon backed by the containerd image-store snapshotter,
  `docker save` was found to exit 0 while silently writing a tar
  containing only its own top-level manifest blob — every layer and the
  config blob it references were absent. A same-host `docker load` check
  didn't catch it, since it resolves those blobs from the daemon's local
  content store instead of the tar itself. The script now parses each
  tar's own OCI manifest and confirms every referenced blob is present at
  the declared size, failing the export on the connected side instead of
  shipping a bundle that only fails once it's on the disconnected side of
  the air gap. `docs/releasing.md` documents the host prerequisite
  (classic `overlay2` driver, not the containerd snapshotter) and the
  `daemon.json` change to get there.

## [0.4.0] - 2026-08-04

### Changed

- orchestration-mcp migrated from mcp SDK 1.x to 2.x (#288): `FastMCP` is
  now `MCPServer` (the module mcp 2.0 removed was why #205 pinned <2.0),
  `transport_security` rides on `streamable_http_app()` instead of the
  constructor, and the tool reads the bearer via 2.x's `Context.headers`.
  Externally visible behavior is unchanged: same /mcp, /health, /metrics,
  and /debug/rag_search routes, same RFC 6750 401 challenge on an
  expired/missing bearer, same tool schema bounds. The dependency pin is
  now `mcp>=2.0,<3.0` (deliberately not straddling the API break), which
  re-opens the SDK to upstream security patches.

### Added

- `PII_LLM_MODEL`, when enabled, now also verifies Phase 1's own regex PII
  findings (#378), not just adding new context-dependent ones (#343): a
  numeric-heavy document (a manual full of part numbers, page references,
  revision codes) turned out to trip the checksum-validated credit-card/
  bank-routing patterns often in practice, even though any one match is
  individually unlikely by chance. The model reads only the already-redacted
  `context` excerpt already shown to the curator (never the raw document
  text) and annotates each finding with an `llm_verdict`
  (`likely_false_positive` + a short rationale) on the `/curate` page --
  this never hides or filters a finding, the curator still sees and decides
  on every regex match. New metric:
  `nexus_rag_ingestion_worker_pii_llm_verification_total{outcome=...}`.
- `scripts/calibrate_tagging_advisory.py` now scores #378's `llm_verdict`
  against real curator decisions (#380): a new `pii_regex_llm_verdict` tally
  reports `agreement_rate` for documents where every PII finding landed on
  the same verdict (all likely-false-positive, or all not) -- did the
  curator's approve/reject/correct decision agree with what the verdict
  predicted? Mixed-verdict or partially-verified documents are counted in
  `skipped` rather than scored. Participates in `--min-agreement` like the
  other agreement-rate suggesters.
- Mutation testing is now an enforced gate (#78): the nightly `mutation` job
  fails below an 80% kill rate on the four security-critical modules
  (`claims.py`, `qdrant_filters.py`, `metadata.py`, `versioning.py`),
  checked by `scripts/check_mutation_score.py`, which also fails closed on a
  crashed/unparseable run -- the advisory era never once produced a score
  and nothing noticed. Baseline at enforcement: 88.0% (183 mutants, 161
  killed); all 22 survivors triaged into strengthened tests, including a
  real gap where nothing asserted `parse_claims` populates `groups` (the
  need-to-know input to the FR-26 filter).

- Chat-plane boundary decision recorded (#286): a purge destroys every copy
  this system holds, but conversations that retrieved the document keep its
  text in LibreChat/LiteLLM stores purge cannot reach. The `document.purged`
  audit event now carries `chat_plane_action_required` and the
  retrievability-window start (`retrievable_since`) and reaches the SIEM via
  the existing NFR-2 export, so chat-plane operators can trigger their side
  of a spillage response; `docs/chat-plane-purge.md` is their runbook, and
  `docs/governance.md` / `docs/roles-and-permissions.md` (new G7) /
  `docs/threat-model.md` state the accepted risk plainly. The flag is backed
  by a new `documents.first_approved_at` column (additive, auto-added on
  startup): set at first approval and deliberately never cleared, so a
  document that was approved, then demoted back to review by an
  out-of-authority tag edit (#268), then purged still triggers the sweep —
  status alone would have missed exactly the misclassify-correct-purge
  sequence purges exist for (caught in review).

- Bulk document upload: the ingestion UI and `POST /documents/batch` accept
  multiple files sharing one Classification/Releasability/Access-scope/
  Source-Originator/Doc-type payload, validated once against the submitter's
  claims rather than per file. Each file is still stored, embedded, and
  curator-reviewed independently, so one file's rejection doesn't fail the
  rest of the batch -- including an infra-level failure (object store, DB)
  partway through, not just a bad file type or empty file. `MAX_BATCH_FILES`
  (default 25) is configurable the same way `MAX_UPLOAD_BYTES` already was
  (`ingestionApi.maxBatchFiles` in the Helm chart). The chart's ingress now
  sizes `proxy-body-size` as `maxBatchFiles x maxUploadBytes` rather than a
  static single-file value, since a batch's whole multipart body lands in
  one request (FR-34, #356).
- A re-embedding path for a stale embedding-model collection (#122, #362):
  `common/qdrant_store.replace_document_chunks` upserts a document's
  freshly re-embedded chunks under the same deterministic point ids
  ingestion uses (new-before-old, mirroring FR-7 supersession), and
  `ingestion-worker`'s new `python -m app.reembed [classification...]
  [--force] [--dry-run]` CLI is the operator-triggered remedy for the
  embedding-model-mismatch refusal #130 shipped detection for but no fix.
  Idempotent (skips a document whose stamped model already matches) and
  scoped to `approved`/`pending_review` documents; run inside the
  ingestion-worker container, not wired into the read path or the
  JetStream consumer.
- Audit query rows now carry a `trace_id` (#134, #363): `rag_search`'s
  audit call writes the current trace's id (when tracing is enabled and
  the request was sampled -- omitted otherwise) into every outcome
  (success, empty, unavailable, denied, embedding-model mismatch),
  correlating an audit row back to its Tempo trace the same way chunk
  provenance already was.

### Fixed

- Helm chart's `MAX_UPLOAD_BYTES` no longer renders as scientific
  notation (#358): Helm's YAML->JSON->Go float64 round-trip formatted a
  large round default like `52428800` as `5.24288e+07`, which
  `int(MAX_UPLOAD_BYTES)` rejects, crashing ingestion-api on startup with
  the chart's own default. Cast to int64 before quoting in the template.
- `docker compose up` self-heals host-umask permission failures (#192):
  a checkout under a restrictive umask (e.g. 077) leaves every non-executable
  tracked file 0600/0700, which several bind-mounted images
  (Postgres, NATS, the Prometheus/Grafana stack, Keycloak) can't read as
  their non-root users -- Postgres and friends failed outright, Grafana
  silently provisioned no datasources/dashboards. A new `fix-config-perms`
  one-shot normalizes `infra/` (excluding runtime-generated `infra/certs`)
  before any dependent service starts.

### Security

- Bumped `cryptography` 49.0.0 -> 50.0.0 in all four service lockfiles
  (#371): fixes CVE-2026-69247 (HIGH), a Bleichenbacher oracle in PKCS#7
  EnvelopedData decryption through distinguishable errors.

## [0.3.0] - 2026-08-03

### Added

- Sensitive-data-pattern curator advisory: ingestion-time regex scan for
  US SSN, Luhn-valid credit card numbers, checksum-valid bank routing
  numbers, API keys/tokens, and private-key blocks, surfaced on the
  curation review page alongside the existing marking-mismatch/
  hidden-instruction advisories. Flag-only — never redacts, blocks, or
  decides; a curator still makes the call (#342).
- LLM-assisted follow-on pass for the sensitive-data-pattern advisory above:
  off by default (`PII_LLM_MODEL`), asks the in-cluster model to flag
  context-dependent sensitive personal/financial information the regex
  scan can't catch (a spelled-out SSN, a foreign national ID, freeform
  PII in prose), surfaced alongside the regex findings on the curation
  review page. Same flag-only posture as #342 — never redacts, blocks, or
  decides (#343).
- Wired the sensitive-data-pattern advisories (#342 regex, #343 LLM-assisted)
  into the existing curator-agreement calibration loop: finding kinds/counts
  now ride along on the approve/reject audit entry, and
  `calibrate_tagging_advisory.py` reports a `pii_regex`/`pii_llm`
  "acted on vs. approved unchanged" rate for each, alongside the
  classification-tag suggesters it already covered (#345).
- Pyroscope, deployed in the opt-in `observability` Compose profile
  (`docker compose --profile observability up -d`), the last piece of
  #133's stack alongside Prometheus/Loki/Tempo/Alertmanager — wired as a
  Grafana datasource, empty until the services push it data (#133).
- All four services (`ingestion-api`, `ingestion-worker`,
  `orchestration-mcp`, `reranker-service`) now push continuous, CPU-only
  profiles to Pyroscope when `PYROSCOPE_SERVER_ADDRESS` is set — off by
  default, same posture as tracing. `service_name` matches the
  `service.name` tracing already uses, so Grafana can jump from a trace
  straight to the flame graph for the same request (#349).

### Fixed

- `rag_search`'s audit-logged `applied_filter` reported `collections` as every
  collection an allowed classification *could* resolve to, including ones
  `hybrid_query` actually skips because they don't exist yet (no approved
  documents at that level) — overstating what was searched in the one place
  (FR-31 audit evidence) that overstatement is dangerous. Now reports both
  `collections_eligible` and `collections_queried` (#272).
- Notifications: the unread-row highlight was a hardcoded `#fff8e1` inline
  style, so it stayed light-yellow under every portal theme — unreadable
  against the near-white body text of the dark themes. Now a `.row.unread`
  rule keyed off the same `--warning`/`--warning-soft` tokens every theme
  keeps stable (#337, #338).
- Portal pages showed a spurious vertical scrollbar and misjudged their own
  height even when content fit in one viewport — the sticky-footer layout
  subtracted a fixed `150px` for header/footer from `100vh` but never
  accounted for the top/bottom classification banners added later (#166),
  so every page ran taller than the viewport by roughly the banners'
  combined height. Now a flex-column body sizes the content area to fill
  whatever space the header, footer, and banners actually leave (#340).

## [0.2.0] - 2026-08-01

### Added

- Curator content view: `/curate/{id}/content` lets a curator read a
  pending document's actual parsed chunk text before approving it, instead
  of reviewing only filename/tags/advisories (#284).
- Hidden-instruction content advisory: ingestion-time scan for invisible/
  control Unicode (including Unicode Tag "ASCII smuggling") and common
  prompt-injection trigger phrases, surfaced in the same tagging advisory
  box as the marking-mismatch/precedent/LLM findings (#284).
- Tagging advisory Phase 2: precedent suggestion via kNN over the approved
  corpus, surfacing similar approved documents' classification/
  releasability as a curator reference (#307).
- Tagging advisory Phase 3: opt-in LLM zero-shot classification suggestion
  against the admin-configured classification vocabulary, off by default
  (`CLASSIFICATION_MODEL`) (#308).
- Tagging advisory Phase 4: `scripts/calibrate_tagging_advisory.py`
  reports how often each suggester (marking-mismatch, precedent, LLM,
  releasability-caveat) agreed with the curator's final decision, mined
  from existing audit entries; run via the new `calibration` compose
  profile (#309).
- Content-hash tamper-evidence (NFR-18): uploaded bytes are SHA-256'd at
  ingestion and re-verified before parsing, failing permanently on a
  mismatch; the digest is carried in submit/approve/reject/embedded audit
  entries (#285).
- Role-gated in-app knowledge base in the ingestion-api web app (FR-33)
  (#305).
- Browser-level CSRF/logout verification: a Playwright script drives a
  real Chromium session against the live stack, now gating CI alongside
  the golden-query job (#187).

### Fixed

- Curation authority checks now consistently resolve existence, then
  curator authority, then document status/org/classification — closing
  several existence- and status-oracle leaks where a curator without
  authority over a document (wrong org, or authority only over a
  supersession's new-but-not-old version) could learn its status or
  classification from an error message before the authority check ran
  (#322, #325, #326).
- `curate_list.html` and `notifications.html` had their page-load script
  in a template block that ran before `base.html` defined the functions
  it called, so both pages threw on every load and never populated
  (#323). Fixed alongside: `GET /notifications`'s page route was shadowed
  by the JSON API route registered at the same path, making the page
  permanently unreachable regardless (#328).
- Fresh-volume deployments deadlocked: a new additive DB column
  (`_ADDITIVE_COLUMNS`) requires table ownership the service role doesn't
  have post-grants-lockdown, and lock-down-db-grants itself waits on
  ingestion-api being healthy — neither could go first. A new
  `migrate-db-schema` one-shot now applies schema as the bootstrap
  superuser before either service starts (#314, #317).
- Concurrent requests during `lock-down-db-grants` could hit a genuine
  `InsufficientPrivilege` in the window between its REVOKE and re-GRANT
  running as separate transactions; both now run inside one transaction
  (#319).

## [0.1.0] - 2026-07-31

First tagged release. 0.1.0 is the version the chart and service packages
have carried as a placeholder since the repo began; this release turns it
into a real, reproducible artifact set: four images on GHCR under immutable
version tags, the Helm chart as an OCI artifact, SBOMs, and an air-gap
export bundle (#295). Everything below summarizes the stack as it stands —
the git history and issue tracker are the authoritative detail.

### The stack at 0.1.0

- Document ingestion with mandatory classification/releasability tagging
  validated server-side against OIDC claims (FR-18), durable original storage
  (NFR-12), and crash-safe queued processing over NATS JetStream (NFR-11).
- Multi-format parsing (PDF/DOCX/PPTX/XLSX/HTML/MD), table- and
  section-aware chunking, OCR for scanned/image content (#241), and opt-in
  VLM image captioning (#92).
- Curator review as the retrievability gate (FR-11/FR-12), org-scoped with
  clearance/releasability/access-scope authority checks (#215, #273, #277),
  marking-mismatch advisories (#138 phase 1), and supersession without a
  visibility gap (FR-7).
- Hybrid retrieval (dense + BM25, RRF-fused, cross-encoder reranked) behind
  a mandatory claims-derived access filter on both legs (FR-26), split into
  per-classification collections (#229), exposed to LibreChat as the
  `rag_search` MCP tool with prompt-injection delimiters (#97).
- Audited purge with an optional two-person request/confirm flow
  (#123, #279), identity-keyed audit logging that stores no query text
  (FR-31, #125), and SIEM export (NFR-2).
- Optional Milvus vector-store backend (#160), optional observability stack
  (#133), Helm chart with NetworkPolicies and per-service hardening (#110,
  #111), and a golden-query retrieval evaluation harness that fails CI on
  recall misses and access-control leaks (FR-26/FR-30).
- Release process: lockstep semver, tag-triggered image/chart publishing,
  version-consistency CI guard, and the air-gapped export bundle (#295).
