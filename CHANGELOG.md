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
