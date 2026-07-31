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
