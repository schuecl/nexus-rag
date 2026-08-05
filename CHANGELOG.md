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
