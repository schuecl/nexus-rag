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

_Nothing yet._

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
