# AI system inventory, vendor assessments, and provenance/licensing register

**RMF outcomes served:** GOVERN 1.6 (inventory), GOVERN 6 / MAP 4 (third-party risk),
MAP 1.6 (technical context).
**Version anchored:** application 0.5.0 (lockstep across chart/pyprojects/image tags,
enforced by `scripts/check_version_consistency.py`).
**Enforcement:** compose-file and Dockerfile image pins are CI-enforced
(`scripts/check_pinned_images.py`, NFR-16) and model revisions likewise
(`scripts/check_pinned_models.py`), both in the blocking `pin-check` job; the
chart's first-party image tags are enforced by
`scripts/check_version_consistency.py`. Chart-side **third-party** tags
(Qdrant, Milvus, NATS, Ollama, SeaweedFS in `values.yaml`) are review-enforced
only — an honest gap in the pin-check surface. This document deliberately lists the *sources of truth*
(compose file, `helm/nexus-rag/values.yaml`) rather than restating every tag, so it
cannot drift from them; only origin/license/assessment columns live here.

## 1. System identity

| Field | Value |
|---|---|
| System | MPNexus RAG pipeline (ingestion, curation, access-controlled retrieval) |
| Application components | `ingestion-api`, `ingestion-worker`, `orchestration-mcp`, `reranker-service` (this repo, version-lockstep) |
| Shared library | `services/common` (claims, filters, metadata, purge, SIEM export) |
| Accountable owner | TBD (organizational) — issue #519 |
| Deployment contexts | Dev/e2e: Docker Compose (NFR-9). Production: Helm chart into existing air-gapped K8s (NFR-10) |
| Out of boundary | LibreChat, LiteLLM, vLLM/Ollama generation, Keycloak — existing MPNexus components, integrated by configuration (C7) |

## 2. Third-party runtime components

Pinned tags live in `docker-compose.yml` and `helm/nexus-rag/values.yaml`; this
table records origin and license, which the pin checks don't cover.

| Component | Role | Maintainer of record | License |
|---|---|---|---|
| Qdrant | Vector store (default backend) | Qdrant Solutions GmbH (Germany) | Apache-2.0 |
| Milvus | Vector store (alternative backend, chart option) | LF AI & Data Foundation project | Apache-2.0 — **see assessment note A3** |
| PostgreSQL | Transactional system of record | PostgreSQL Global Development Group | PostgreSQL License |
| NATS JetStream | Durable ingestion queue (NFR-11) | Synadia / CNCF | Apache-2.0 |
| SeaweedFS | Object store for originals (NFR-12) — chart option; a deployment can point at an existing S3 endpoint instead | Chris Lu / community (US) | Apache-2.0 |
| Ollama | Embedding-model serving (dedicated instance) | Ollama Inc. (US) | MIT |
| Keycloak (dev realm) | OIDC provider (production instance is platform-owned) | Red Hat | Apache-2.0 |
| Prometheus stack (prometheus, alertmanager, pushgateway, exporters, blackbox) | Observability | Prometheus/CNCF | Apache-2.0 |
| Grafana observability profile (dev/opt-in): Grafana, Loki, Tempo, Pyroscope | Dev observability (`observability` compose profile / `helm/observability`) | Grafana Labs | **AGPL-3.0** (Alloy/OTel Collector components Apache-2.0) |

## 3. Models

| Model | Stage | Serving | Origin | License | Provenance note |
|---|---|---|---|---|---|
| `nomic-embed-text` | Dense embeddings (ingest + query) | Ollama (dedicated) | Nomic AI (US) | Apache-2.0 | Selected under C2 screening (`REQUIREMENTS.md` §7.2); embedding provenance stamped per point and verified at read, refuse-on-mismatch (issue #122; prefix-scheme extension #392) |
| `cross-encoder/ms-marco-MiniLM-L6-v2` | Reranking | `reranker-service` (HF cache, revision-pinned) | sentence-transformers / MS MARCO (Microsoft dataset) | Apache-2.0 | §7.3 candidate, adopted |
| `Qdrant/bm25` | Sparse/BM25 leg | fastembed (revision-pinned) | Qdrant (Germany) | Apache-2.0 | `services/common/common/sparse_embedding.py` |
| `qwen2.5:7b-instruct` | **Dev-stack default generation model** (downstream of boundary, C7) | Throwaway dev LibreChat path | Alibaba (China) | Apache-2.0 | **Assessment note A1 — C2 tension, decision required** |
| `qwen2.5:3b-instruct` | **Default local judge** for `evaluate_rag_quality.py` (also the `.env.example` dev generation default; compose falls back to the 7B when unset) | Ollama, eval host | Alibaba (China) | **Qwen Research License (non-commercial)** — unlike the 7B, not Apache-2.0 | **Assessment note A1** — used by this project's own tooling, not just downstream |
| `VISION_MODEL` | Opt-in image/figure captioning (#92) | Ollama; empty-disables default | Operator-chosen | — | **Not advisory-only**: captions enter the retrievable corpus through the normal chunk/embed pipeline, passing curator review with the document |
| `CLASSIFICATION_MODEL` / `PII_LLM_MODEL` | Opt-in advisory passes (classification suggestion, PII advisory) | Ollama; empty-disables default | Operator-chosen | — | Advisory-only posture: outputs are curator-facing hints, never enforcement (issues #308, #342/#343) |

## 4. Vendor and model-provider assessments

Screening criteria come from `REQUIREMENTS.md` §3: C1 (open-source/free only, no
paid tiers), C2 (no Chinese-sourced products or models — applies to training
organization and maintainer of record), C4 (mirrorable into the air-gapped
registry). Every §7 candidate table records the screening result; named exclusions
(BAAI bge-*, Alibaba Qwen embeddings, RAGFlow, FastGPT, Onyx EE) are documented
with reasons. That screening *is* the vendor-assessment process; this section
records the residuals it left open.

**A1 — qwen2.5 defaults vs C2 (decision required — TBD, organizational).**
The dev stack defaults generation to qwen2.5 (`3b-instruct` via `.env.example`,
`7b-instruct` as the compose fallback) and the Q→C→A evaluator defaults its
judge to `qwen2.5:3b-instruct` — all Alibaba-origin, which C2 excludes, while
`REQUIREMENTS.md` §3's preamble applies every constraint, including C2, to
"every component in this project." The 3B is additionally under the **Qwen
Research License (non-commercial)**, not Apache-2.0 — a C1 strike on top of
the C2 one, strengthening option (b) below. Mitigating context, honestly stated: generation is outside the
project boundary (C7) and this default exists only in the throwaway dev stack;
the judge runs on the eval host, its scores are explicitly comparative-only, and
it never touches the production path; both are trivially operator-overridable env
vars. But neither doc currently *records* that reasoning as a decision. Options:
(a) write the exception rationale into REQUIREMENTS.md §3/C2, or (b) swap the
defaults for a C2-clean pair (e.g. a Mistral- or Llama-family instruct model,
subject to their licenses). Until decided, this row is the inventory's one open
screening finding.

**A2 — production OIDC and generation stack are platform-assessed, not
project-assessed.** Keycloak, LibreChat, LiteLLM, vLLM versions in production
belong to MPNexus platform management; this inventory covers only the dev-realm
copies this repo ships. The platform's own assessment evidence is outside this
package — recorded so the audit boundary is explicit.

**A3 — Milvus backend option.** The chart offers Milvus (LF AI & Data Foundation)
as an alternative vector backend. Milvus originated at Zilliz (China-founded);
its maintainer of record today is the LF AI & Data Foundation. Whether that
satisfies C2's "maintainer of record" test is a deployment decision that should
be recorded before the Milvus path is used in production; the default Qdrant
path has no such question.

## 5. Data provenance register (corpus)

Per-document provenance is a live system property, not a static register — which
is stronger evidence than a spreadsheet:

- **Source binding:** original bytes stored durably (NFR-12), SHA-256 fingerprinted
  at submission, re-verified by the worker before parsing, digest bound into the
  audit trail for every curation decision (NFR-18).
- **Transformation lineage:** embedding provenance stamp written with every point
  and verified at read (refuse-on-mismatch); `docs/governance.md` "Lineage and
  provenance" documents the chain *and its known limits* (reranker version,
  chunking parameters, and filter version are not stamped at write time).
- **Decision lineage:** who uploaded, who approved/rejected/corrected, with what
  tags, when — append-only audit log (FR-31/NFR-2, enforced against live grants).
- **Query lineage:** retrieval audit rows carry a trace_id when tracing is
  enabled and the request was sampled (default 5%, issue #134; 100% in the dev
  stack) — absent otherwise by design, so a row never carries a dead
  correlation key.

## 6. Licensing register

| Asset class | Status |
|---|---|
| Third-party software | Licenses per component in §2; per-release CycloneDX SBOMs (attached to GitHub Releases, regenerated weekly by security.yml) are the only machine-readable component record — **no dedicated dependency-license gate exists in CI** (the README's License badge is a static badge for this repo's own Apache-2.0 license; it tracks nothing) |
| Model weights | Licenses per model in §3 (all Apache-2.0/MIT for the production path) |
| **Ingested corpus content** | **Gap.** Nothing records the license or distribution terms of uploaded documents. For the intended corpus (government regulations, SOPs, manuals — works of the U.S. government or internally-produced) this is low-risk, but the assumption is not enforced or recorded at ingest. Candidate fix if ever needed: a `license/distribution-terms` field in the §6.3 metadata schema — deliberately *not* proposed as work now; recorded as an accepted scoping decision pending deployment-owner review |
