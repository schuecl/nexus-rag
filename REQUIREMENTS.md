# REQUIREMENTS.md — MPNexus RAG Pipeline

**Project:** Enterprise Retrieval-Augmented Generation (RAG) capability for MPNexus
**Status:** Draft — requirements gathering
**Owner:** [Corey / MPNexus platform]
**Last updated:** 2026-07-22

---

## 1. Purpose

MPNexus (LibreChat + LiteLLM + vLLM/Ollama, running in an air-gapped Kubernetes cluster for USAREUR-AF) needs a production-grade RAG pipeline so that users can query organizational documents — regulations, SOPs, technical manuals, product data sheets, etc. — with answers grounded in retrieved source material instead of model memory alone. This document captures the requirements, constraints, and candidate technology options for that capability. It intentionally separates **what the system must do** (requirements) from **how it will be built** (design), though a candidate stack is proposed in Section 7 to support planning.

## 2. Background

The initiating reference is Gautam Vhavle's ["Building RAG Systems: From Zero to Hero"](https://dev.to/gautamvhavle/building-production-rag-systems-from-zero-to-hero-2f1i), a practitioner write-up of lessons learned building several RAG systems. The parts most relevant to MPNexus:

- A RAG pipeline has two phases — **ingestion** (collect → chunk → embed → store) and **retrieval/generation** (embed query → search → rerank → generate) — and production quality lives almost entirely in the details of each phase, not the concept.
- **Chunking strategy matters more than it looks.** Fixed-size chunking is easy but breaks semantic units; the article's practical recommendation is to start around 512 tokens with ~15% overlap and respect natural document boundaries (headings, sections) rather than raw token counts alone.
- **Metadata filtering is not optional.** The article calls out "ignoring metadata" as one of its costliest early mistakes — pure semantic search without filters (date, source, category) surfaced stale or wrong-context results. This maps directly to our Classification/Releasability requirement below: metadata filtering is the mechanism, not an add-on.
- **Hybrid retrieval (dense + sparse/BM25) consistently outperforms pure semantic search**, especially in domains with specific jargon, product names, or codes — which describes most DoD/technical documentation.
- **Reranking** (a cross-encoder pass over the top-N retrieved chunks) was described as a bigger accuracy lever than the author expected and should be treated as standard, not optional.
- **Small, self-hosted models paired with good retrieval can match or beat larger general-purpose models on domain-specific tasks**, at a fraction of the latency and cost — this validates the existing MPNexus approach of self-hosting via vLLM/Ollama rather than depending on a hosted frontier model.
- **Production RAG requires monitoring**, not "set it and forget it" — retrieval quality silently degrades as the corpus grows; retrieval metrics (recall@K, precision@K, faithfulness) need to be tracked over time.

None of the article's example code or specific product recommendations are prescriptive for MPNexus — several of its top suggestions (e.g., OpenAI embeddings, BAAI/Qwen-family open models) are excluded here by the origin constraint in Section 3. Section 7 proposes MPNexus-appropriate substitutes that satisfy the same design lessons.

## 3. Guiding Constraints

These apply to every component in this project, not just the ones explicitly named below.

| # | Constraint | Notes |
|---|---|---|
| C1 | **Open-source or free software and models only — no budget for paid tiers or licenses** | No proprietary SaaS APIs (OpenAI, Cohere, Anthropic API, etc.) for any pipeline stage, and no commercial/Enterprise-Edition upgrades either — the project has no funding for licensing. Self-hostable, inspectable, and redistributable within the air-gapped enclave using free tiers only. |
| C2 | **No Chinese-sourced products or models** | Excludes, among others: Alibaba/Qwen (embeddings, models), BAAI (bge-*, including bge-reranker), DeepSeek, Zhipu/GLM, InfiniFlow/RAGFlow, LabRing/FastGPT. Applies to the model weights' training organization and to the software vendor/maintainer of record, not just the hosting location. Every candidate in Section 7 has been checked against this. |
| C3 | **Qdrant as the vector database** | Already decided. Rust-based, Apache 2.0 license, maintained by Qdrant Solutions GmbH (Germany) — satisfies C1 and C2. |
| C4 | **Runs on a US military network** | Data tagging, classification handling, RBAC, and security are top priorities — see Section 6. Assume no outbound internet access; every component and model must be mirrorable into the air-gapped registry MPNexus already uses. |
| C5 | **Web UI for document ingestion** | Non-technical users need a way to drop documents into the pipeline without touching a CLI or API directly. |
| C6 | **Mandatory metadata tagging at ingestion** | Every ingested item must be tagged at minimum with **Classification** and **Releasability**, and those tags must be enforced at retrieval time, not just stored for display. |
| C7 | **Integrates with existing MPNexus stack** | LibreChat (chat UI), LiteLLM (AI gateway), vLLM and Ollama (inference) are fixed. The RAG pipeline is a new capability layered onto this, not a replacement for it. |
| C8 | **Ingestion Web UI and tagging are OIDC-based** | The ingestion front end authenticates through the same OIDC provider as LibreChat. The Classification/Releasability values a user may assign at upload — and the values enforced on their behalf at query time — are both derived from that user's OIDC claims, not freely chosen or client-supplied. See Section 6. |
| C9 | **Classification/Releasability lists are admin-configurable** | The system supports a list of possible Classification values and a list of possible Releasability values, each document taking exactly one of each (Section 6.3) — but the *lists themselves* are maintained by an admin through configuration, not hardcoded, so values can be added, retired, or reordered without a code change. |

## 4. Functional Requirements

### 4.1 Ingestion Pipeline
- FR-1: Provide a web UI where an authorized user can upload one or more documents for processing (PDF, DOCX, PPTX, XLSX, TXT/MD, HTML at minimum).
- FR-2: At upload time, require the user to complete the metadata schema in Section 6.3 before the document can be submitted for processing — no silent defaults for Classification or Releasability. Submission itself is self-service (see Section 4.2 for what happens next).
- FR-3: Parse each document into clean text, preserving structural signal (headings, page/slide numbers, table boundaries) for use in chunking and citation.
- FR-4: Chunk parsed text using a strategy that respects document structure (section/heading boundaries) rather than pure fixed-token splitting, with configurable target chunk size and overlap (starting point: ~512 tokens, ~15% overlap, per Section 2).
- FR-5: Generate embeddings for each chunk using a self-hosted, non-Chinese-origin embedding model (see Section 7.2).
- FR-6: Store each chunk's vector and its full metadata payload (Section 6.3), including a `pending_review` status, in Qdrant.
- FR-7: Support re-ingestion / versioning — replacing an outdated document's vectors without leaving orphaned or duplicate entries, and preserving an audit trail of what was replaced and by whom.
- FR-8: Provide ingestion status/progress feedback in the UI (queued, processing, embedded, pending review, approved, rejected-with-reason).
- FR-9: Reject or quarantine files that fail parsing, are password-protected, or exceed a configurable size limit, with a clear error surfaced to the uploader.

### 4.2 Curation & Review Workflow
- FR-10: Submission is self-service — any user holding the `rag-ingest` role (Section 6.2) can submit documents without needing prior approval to begin processing.
- FR-11: Every self-service submission is chunked and embedded immediately but enters a `pending_review` status, excluded from retrieval (FR-26) until a curator approves it — this is the control against spillage from incorrect Classification, Releasability, or Access-scope tagging.
- FR-12: A dedicated curator role (Section 6.2) can view a queue of pending submissions — scoped to the org(s) that user holds curator authority for, not every submission system-wide — along with the tags the uploader assigned.
- FR-13: A curator can **approve** (publish — status becomes `approved` and the document becomes retrievable), **reject** (status becomes `rejected`, never published, with a required reason returned to the uploader), or **correct** the Classification/Releasability/Access-scope tags before approving.
- FR-14: A curator's authority is capped two ways: (1) by clearance/releasability, same as FR-18 — a curator cannot approve a document tagged above their own authorized level; and (2) by org — a curator can only review submissions whose Source/Originator org (Section 6.3) matches one of the orgs they hold curator authority for (Section 6.2).
- FR-15: The uploader is notified of the curator's decision (approved, or rejected with the stated reason).
- FR-16: Curator decisions — who reviewed, which document, what tags, approve/reject/correct, and when — are captured in the audit log (FR-31).

### 4.3 Metadata & Classification Tagging
- FR-17: Metadata fields are structured and validated (controlled vocabularies / dropdowns for Classification and Releasability), not free text, to prevent tagging drift.
- FR-18: The Classification and Releasability values offered to a user at upload are constrained to what their authenticated OIDC claims authorize — a user cannot select, and the UI should not even display, a value above their own cleared level. This is a UI-level guardrail; FR-26/6.1 covers the non-bypassable enforcement.
- FR-19: Identity-linked fields (Owner/POC, uploading organization) auto-populate from the authenticated user's OIDC claims rather than free-text entry, and are not editable by the uploader.
- FR-20: Every document carries exactly one Classification value and one Releasability value, each chosen from the admin-configurable controlled lists in Section 6.3 — all of a document's chunks inherit that single pair; there is no mixed-sensitivity or chunk-level override.
- FR-21: Metadata must be visible to end users in retrieval results (e.g., a citation shows source, classification, and releasability alongside the answer).
- FR-22: Support document-level access scoping by Organization, Group, and individual User, independent of and in addition to Classification/Releasability (see Section 6.3's Access scope field) — a document can be restricted to one or more orgs/groups/users, not just gated by classification.
- FR-23: Provide a reserved `PUBLIC` access-scope value that makes a document visible to every authenticated user regardless of Org/Group/User membership, without bypassing Classification/Releasability filtering.

### 4.4 Retrieval & Generation
- FR-24: Support hybrid retrieval — dense (vector) search combined with sparse/keyword (BM25) search — with results merged/fused rather than dense-only.
- FR-25: Apply a reranking pass (cross-encoder) over the top-N hybrid candidates before the final top-K is handed to the LLM.
- FR-26: Every retrieval query must be filtered by the requesting user's authorized Classification level, Releasability, and Org/Group/User access scope (FR-22/FR-23), and must exclude any document not in `approved` status (FR-11) — a user only sees documents they are both cleared for and explicitly granted (or marked `PUBLIC`) access to. All of this is sourced from the same OIDC claims used to constrain tagging in FR-18, and enforced server-side before results are returned — never client-side or advisory-only (see Section 6.1 for why this can't rely on Qdrant's JWT layer alone).
- FR-27: Cited sources (document name, classification, releasability, and location within the document) must be returned alongside generated answers so users can verify grounding.
- FR-28: When retrieval confidence is low or no results pass the access filter, the system should say so rather than letting the LLM answer from unguided memory.
- FR-29: Retrieval and generation are exposed to LibreChat as a custom MCP server tool (not LibreChat's built-in per-conversation file-upload RAG feature, which is a different thing with a similar name) — consistent with how PING search and the existing Cisco SSH/diagram-generation MCP servers are already exposed. LibreChat forwards the user's Keycloak JWT to this MCP server per Section 6.1, which is what makes FR-26's enforcement possible without extra middleware.

### 4.5 Monitoring & Evaluation
- FR-30: Track retrieval quality metrics over time (recall@K, precision@K, or an equivalent proxy) so degradation as the corpus grows is visible, not silent.
- FR-31: Log every ingestion, curation, and retrieval event (who, what, when, which classification/releasability/access-scope filters were applied or which curation decision was made) for audit purposes, keyed on the actor's OIDC identity (`sub`/`preferred_username`), not a self-reported name.
- FR-32: Provide a way to periodically re-evaluate the pipeline against a fixed set of test queries to catch regressions after model, chunking, or reranker changes.

## 5. Non-Functional Requirements

- NFR-1: **Air-gapped operation.** No component may require outbound internet access at runtime. All models and packages must be mirrorable into the existing offline registry.
- NFR-2: **Auditability.** Every ingestion, tagging, and retrieval action is logged with actor identity, timestamp, and outcome, consistent with DISA STIG expectations for the environment.
- NFR-3: **Least privilege / RBAC.** Ingestion (write), curation (review/approve), and query (read) permissions are separately assignable via distinct roles (`rag-ingest`, `rag-curate`, `rag-query`); not every user who can query should be able to ingest or curate, and vice versa.
- NFR-4: **Performance.** Target end-to-end query latency (retrieval + rerank + generation) should be defined once a latency budget is agreed — flag as an open question in Section 8. Compute headroom is not the constraint (NFR-8).
- NFR-5: **Scalability.** The design should not assume a fixed corpus size; re-indexing or incremental updates must not require full pipeline downtime.
- NFR-6: **Encryption at rest.** Vector store and raw document storage should support encryption at rest; MPNexus's existing PyKMIP deployment is a candidate key-management integration point.
- NFR-7: **Resilience to bad input.** Malformed or adversarial documents (corrupt PDFs, zip bombs, oversized files) must not crash ingestion workers.
- NFR-8: **Reasonable resource footprint.** Up to 6 of the existing 8×16GB GPUs (96GB VRAM) can be dedicated to this pipeline — embedding, reranking, and any dedicated RAG-serving models — leaving the remaining 2 GPUs for generation workloads already served (see [[mpnexus]] hardware notes). This is a substantially larger allowance than a typical embedding/reranker footprint requires, so hardware is not expected to be the limiting factor.
- NFR-9: **Local development & testing.** A Docker Compose stack must be a self-contained, one-command stand-up of the **entire** stack needed to exercise the full ingest → curate → query flow on a developer workstation with zero dependency on the production cluster — the ingestion UI, the RAG orchestration/MCP service, Qdrant, embedding and reranker model serving, a Keycloak instance, and throwaway LibreChat + LiteLLM instances so the actual MCP tool-calling and OBO token exchange (Section 7.7) can be exercised locally, not just the retrieval mechanics. Everything should be pre-seeded as much as possible — a test Keycloak realm with example users covering each role/clearance/org combination (e.g., ingest-only, query-only, a curator scoped to one org, an admin), plus sample documents already ingested at a range of Classification/Releasability/Access-scope/Status values — so a fresh clone-and-run immediately exercises real RBAC scenarios (allowed query, denied query, pending vs. approved, curator approve/reject) without manual setup.
- NFR-10: **Production packaging.** The production deployment target is the existing air-gapped Kubernetes cluster; the pipeline is packaged as a **Helm chart** scoped to only the *new* components this project adds — the ingestion UI, the RAG orchestration/MCP service, Qdrant, and embedding/reranker model serving. The chart assumes LibreChat, LiteLLM, Keycloak, and vLLM/Ollama already exist and are separately managed in the cluster; it integrates with them via configuration (endpoints, MCP server registration, OIDC client settings) rather than deploying or bundling them. This is a deliberate contrast with NFR-9: Compose stands up everything from scratch for a self-contained dev environment; Helm deploys only the delta into infrastructure that's already there.

## 6. Security, Classification & Access Control

### 6.1 Access Control Architecture — Known Constraint
Qdrant introduced JWT-based RBAC in v1.9 (role assignment, read/write scoping, API-key revocation). However, **fine-grained payload-level filtering enforced inside the JWT itself was deprecated in Qdrant v1.16** because of unresolved conflicts with write operations. Practical implication for this project:

- Treat Qdrant's own RBAC/JWT as **coarse-grained** access control only (which service accounts can read/write which collections).
- **Classification and Releasability filtering must be enforced at the application/orchestration layer** — i.e., in the RAG orchestration service or the LiteLLM-facing middleware — by injecting a mandatory payload filter into every query based on the authenticated user's cleared access level(s), before the query ever reaches Qdrant's HNSW search.
- This should be designed so it's impossible to bypass from the client side: the orchestration layer builds the filter from the user's identity/claims (via OIDC), not from anything the client submits.
- The same principle applies symmetrically on the ingestion side (per C8): the set of Classification/Releasability values a user is permitted to *assign* comes from their OIDC claims, evaluated server-side at submit time — not just hidden/disabled in the UI, which a client could bypass by calling the ingestion API directly. Ingestion-time and query-time enforcement should share one claims-evaluation library/service rather than being implemented twice.
- The same cap applies a third time, to curation (FR-14): a curator's claims are checked the same way before an approval is accepted — a curator cannot publish a document above their own cleared level, even via the review queue.
- **Reference OIDC provider: Keycloak.** Keycloak is the confirmed IdP for this project. Its protocol mappers and client scopes can map arbitrary user attributes (custom or built-in) into ID/access token claims, so every claim in Section 6.2 can be implemented as a Keycloak user attribute without any Keycloak code changes — only admin-console configuration. That said, the enforcement layer itself should be written against standard OIDC/JWT claims, not Keycloak-specific APIs, so it isn't locked to one IdP if that ever changes.
- **Query interface: MCP.** The enforcement layer described above is realized as a custom MCP server (via kmcp/FastMCP, consistent with the existing Cisco SSH and diagram-generation servers) exposed to LibreChat as an agent/tool. LibreChat 0.8.7 (confirmed version in use) supports two ways to forward the authenticated user's identity to an MCP server: `addUserJwtToken: true` (the user's raw token forwarded as-is) or OAuth On-Behalf-Of token exchange, added natively in 0.8.7 for MCP connections (configured via `obo.scopes` in librechat.yaml). **OBO is the recommended choice**: it exchanges the user's session token for a new token scoped specifically to the RAG MCP server's audience, rather than forwarding the same token used for the user's whole LibreChat session to a downstream tool — a meaningfully better security posture for a tool gating classification-tagged content. See Section 7.7 for the prerequisites this requires and Section 9 for how it fits the overall flow.

### 6.2 Proposed OIDC Claims Schema (Keycloak)
Starting point for discussion. Keycloak can issue any of these as custom claims via user attributes + protocol mappers/client scopes — no IdP code changes needed, only admin-console configuration (see Section 6.1):

| Claim | Purpose | Example value |
|---|---|---|
| `clearance` (or equivalent) | Max Classification level the user is cleared for; caps the tagging dropdown (FR-18), the curator's approval authority (FR-14), and the query-time filter (FR-26) | `SECRET` |
| `releasability` | Releasability caveats the user is authorized for | `REL TO USA/FVEY` |
| `groups` (or equivalent) | Org/group memberships used for document-level access scoping (FR-22/FR-23) — a user's query filter includes any document scoped to a group they belong to, their own user ID, or `PUBLIC` | `["USAREUR-AF", "Signal-Corps"]` |
| `rag_roles` | Function roles (`rag-ingest`, `rag-query`) plus org-scoped curator grants, encoded as `rag-curate:<org>` entries — see below | `["rag-ingest", "rag-query", "rag-curate:USAREUR-AF"]` |
| `org`/`unit` | Auto-populates Owner/POC and supports Program/community filtering (FR-19) | `USAREUR-AF` |
| `sub` / `preferred_username` | Standard OIDC identity claim; used for audit logging (FR-31), not a new claim | — |

**Per-org curator assignment (FR-12/FR-14).** Rather than a single flat `rag-curate` role, define one Keycloak **client role** per org that needs curators, named with a consistent, parseable convention — e.g., `rag-curate:USAREUR-AF`, `rag-curate:Signal-Corps` — scoped to the RAG client (not realm roles, to keep this contained to the RAG application). Assign a user one such role per org they're allowed to curate for; someone curating for multiple orgs just holds multiple `rag-curate:<org>` roles. All of these surface in the same `rag_roles` claim alongside the plain function roles, and the RAG service parses the `rag-curate:` prefix to build each user's list of curatable orgs. Adding a new org's curator capability is then a Keycloak admin-console action (create the client role, assign it to the chosen people) — no application change, consistent with C9's spirit of admin-configurability. This also naturally supports a "central curator pool" if wanted: just assign every pool member the `rag-curate:<org>` role for every org. Curator *nomination and control* itself is handled by an existing internal government process outside this project's scope — this design's only job is to make sure whatever that process decides can be expressed as a `rag-curate:<org>` role assignment in Keycloak.

Keycloak resolves the technical question of *whether* these claims can exist. What's still open is *who maintains the values* day to day — i.e., who in Keycloak's admin console keeps each user's `clearance`, `releasability`, `groups`, and per-org curator role attributes current as people's assignments change (see Section 8).

### 6.3 Proposed Metadata Schema (for ingestion tagging)
This is a starting point for discussion, modeled on standard DoD document marking practice (CAPCO-style banner fields) — not a final schema. Classification and Releasability are each a **single value per document** (no multi-select, no chunk-level override), chosen from lists an admin maintains, not hardcoded into the application:

| Field | Example values | Required |
|---|---|---|
| Classification | Single value from an admin-configurable, ranked list — e.g., UNCLASSIFIED < CUI < CONFIDENTIAL < SECRET < TOP SECRET *(configure to the actual network's accreditation level; the rank ordering is what lets FR-18/FR-26 compare "at or below the user's cleared level")* | Yes |
| Releasability | Single value from an admin-configurable list — e.g., NOFORN, REL TO USA/FVEY, REL TO NATO | Yes |
| Access scope | One or more of: specific Organization(s), Group(s), User(s), or the reserved value `PUBLIC` (visible to every authenticated user, still subject to Classification/Releasability) | Yes |
| Status | `pending_review`, `approved`, `rejected` — set by the system/curator, not the uploader (Section 4.2) | System-managed |
| Caveats/SCI controls | Per local guidance | If applicable |
| Source/Originator | Organization or system of record | Yes |
| Document type | Regulation, SOP, manual, data sheet, etc. | Yes |
| Program/community | Free-form or controlled tag for filtering by mission area | Recommended |
| Effective date / review date | Date | Recommended |
| Owner/POC | Uploading org or individual | Recommended |

Access scope and Classification/Releasability are independent dimensions that both apply: a document must pass *both* checks to be retrievable — e.g., a SECRET/REL TO USA/FVEY document scoped to the "Signal Corps" group is only visible to Signal Corps users who are also cleared to that classification and releasability. `PUBLIC` only removes the Org/Group/User restriction, never the Classification/Releasability one, and `approved` status is required regardless of scope (Section 4.2).

An admin-facing configuration screen (or config table) should own the Classification and Releasability lists — adding a new value, retiring one, or changing the Classification rank order should not require a code change or redeploy.

### 6.4 Other Security Requirements
- Authentication via the existing OIDC provider already used for LibreChat login — no separate identity system for the RAG UI, and no separate credential store for classification/releasability (Section 6.2's claims are the single source of truth for both ingestion and retrieval).
- All inter-service traffic (ingestion UI → processing → Qdrant → retrieval → LiteLLM) stays inside the cluster's internal network; nothing is exposed beyond what's already exposed today.
- No Cross-Domain Solution is needed — each document carries a single Classification/Releasability pair (Section 6.3), and the instance is not intended to span multiple classification levels, so there's no cross-domain transfer for this pipeline to handle.

## 7. Candidate Technology Stack

This section proposes options that satisfy the constraints in Section 3. It is a starting menu for evaluation, not a final decision.

### 7.1 Already Decided
| Component | Choice | Origin/License |
|---|---|---|
| Vector database | Qdrant | Germany (Qdrant Solutions GmbH), Apache 2.0 |
| Chat UI | LibreChat | Existing MPNexus component |
| AI gateway | LiteLLM | Existing MPNexus component |
| Inference | vLLM / Ollama | Existing MPNexus component |
| Local dev/test environment | Docker Compose (NFR-9) | Self-contained, one-command stack including throwaway LibreChat/LiteLLM/Keycloak instances — everything, pre-seeded |
| Production packaging | Helm chart (NFR-10) | Scoped to only the new components; assumes LibreChat/LiteLLM/Keycloak/vLLM-Ollama already exist in the cluster |

### 7.2 Embedding Models (self-hosted, non-Chinese origin)
| Model | Origin | License | Notes |
|---|---|---|---|
| `nomic-embed-text-v1.5` | Nomic AI (US) | Apache 2.0 | Matryoshka embeddings, runs natively via Ollama |
| `mxbai-embed-large-v1` | Mixedbread AI (Germany) | Apache 2.0 | Strong MTEB performance for its size |
| `snowflake-arctic-embed-l` | Snowflake (US) | Apache 2.0 | Good general-purpose option |
| `multilingual-e5-large` | Microsoft (US) | MIT | Useful if multilingual source docs are in scope |
| `all-MiniLM-L6-v2` | sentence-transformers project (US/EU academic origin) | Apache 2.0 | Lightweight fallback / CPU-friendly baseline |

**Excluded by C2:** BAAI `bge-*` family, Alibaba `Qwen3-Embedding-*`, and similar — despite strong MTEB rankings, these are Chinese-origin.

### 7.3 Reranker Models
| Model | Origin | Notes |
|---|---|---|
| `cross-encoder/ms-marco-MiniLM-L6-v2` | Microsoft MS MARCO dataset, via sentence-transformers | Well-established, lightweight |
| `mxbai-rerank-large-v1` | Mixedbread AI (Germany) | Apache 2.0 |

**Excluded by C2:** BAAI `bge-reranker-*`.

### 7.4 Document Parsing & Chunking
| Tool | Origin | Notes |
|---|---|---|
| Docling | IBM Research (US) | Strong table/layout-aware PDF parsing |
| Unstructured | Unstructured.io (US) | Broad format coverage (PDF, DOCX, PPTX, HTML, etc.) |
| LangChain / LlamaIndex / Haystack text splitters | US / Germany (deepset) | For structure-aware chunking logic |

### 7.5 Ingestion Web UI / Orchestration Platform

The four capabilities you're asking about — enforced fields at ingest, Org/Group/User document-level RBAC, search scoped to what the user can access, and a "public" share-with-everyone tag — were checked against the leading non-Chinese candidates. Short answer: **none of them satisfy all four out of the box**, and the gaps differ by platform.

| Capability | Onyx (Community Edition) | Onyx (Enterprise Edition — excluded, no budget) | AnythingLLM | Dify |
|---|---|---|---|---|
| Enforced/mandatory fields at ingest | No | No | No | No — flexible custom metadata exists and can drive retrieval filters, but nothing requires a field be filled in before a document is usable |
| Document-level RBAC (Org/Group/User) | No — RBAC in CE covers agents/actions, not per-document access | **Yes** — direct user/group assignment to documents, plus permission mirroring from connectors | No — access is per-workspace (Admin/Manager/Default roles); everyone with workspace access sees every document in it | No — fixed roles (Owner/Admin/Editor/Dataset Operator) at the workspace/dataset level; a dataset can be restricted to a member list, but not down to individual documents |
| Search scoped to what the user can access | No | **Yes** — this is what "permission-aware retrieval" and document sets are for | Only at the workspace boundary | Only at the dataset (knowledge base) boundary |
| "Public" tag shareable with everyone | Approximable via an open workspace/group | Approximable via a group everyone belongs to | Approximable via an open workspace | Easy to model as a metadata value/filter condition, given native metadata filtering |
| License | Free, MIT | **Paid — custom pricing, requires a commercial license/contract; self-hosted offline licensing terms need to be confirmed directly with the vendor** | Free, MIT | Free, modified Apache 2.0 |

Takeaways:
- **Onyx Enterprise Edition is ruled out** — there's no budget for a paid license, so the one candidate that does document-level RBAC and permission-scoped search natively isn't actually available here. Onyx Community Edition remains usable (free, MIT), but only for chat/connectors — the document-level RBAC and search-scoping still have to be built.
- **AnythingLLM and Dify both stop at workspace/dataset-level access**, not per-document. Neither would give you "user A and user B can both search the same document library, but only see the subset each is cleared for" without building that layer yourself on top.
- **Dify's metadata system is the best raw material for tagging** (custom fields, used natively as retrieval filters — including a straightforward "public" boolean or releasability value), but it doesn't *enforce* required fields, and its RBAC is coarser than what you need.
- None of this changes the conclusion from Section 6.1: because Qdrant's own JWT filtering is coarse-grained too, the actual per-document permission check has to be enforced in an application/orchestration layer regardless of which front end you pick. With Onyx EE off the table, that layer is now definitely something you build, not something you buy.

| Option | Origin | Notes |
|---|---|---|
| Custom-built ingestion UI + orchestration API | — | Full control over mandatory tagging, document-level RBAC, and permission-scoped search. The only path that gets all four capabilities within a $0 budget. Consistent with how PING/MPNexus's other custom pieces were built. |
| Onyx Community Edition + custom permission layer | US | Reuse Onyx's chat/connector UI, but the document-level RBAC and permission-scoped search still need to be built, since CE doesn't have them. |
| AnythingLLM | Mintplex Labs (US) | Open source, workspace-level access only; would need a custom permission layer for document-level RBAC. |
| Dify | LangGenius (US) | Open source (modified Apache 2.0 — restricts resale as multi-tenant SaaS, not relevant for internal use); strongest metadata/filtering primitive to build on top of, but RBAC and field enforcement still need custom work. |

**Excluded by C2:** RAGFlow (InfiniFlow, Shanghai), FastGPT (LabRing, China), Coze (ByteDance).
**Excluded by C1 (no budget):** Onyx Enterprise Edition.

> Recommendation: with Onyx Enterprise Edition ruled out by budget, the document-level RBAC, permission-scoped search, and enforced tagging all need to be custom-built on top of an open-source foundation regardless. Dify's native metadata-filtering is the strongest piece to build on top of if you want to reuse an existing ingestion/chunking/retrieval UI rather than writing one from scratch; a fully custom ingestion UI + orchestration API gives the most control but the most build effort. Either way, the permission-enforcement layer described in Section 6.1 is required — that part isn't optional regardless of which UI you start from.

### 7.6 Evaluation / Monitoring
| Tool | Origin | Notes |
|---|---|---|
| RAGAS | Open source (exploding gradients) | Reference-free RAG evaluation (faithfulness, context precision/recall) |

### 7.7 Query Interface
| Component | Choice | Notes |
|---|---|---|
| RAG search tool | Custom MCP server, built with kmcp/FastMCP | Matches the existing pattern (Cisco SSH server, diagram-generation server); exposed to LibreChat as an agent/tool, not a separate UI. |
| User identity propagation | OAuth On-Behalf-Of (OBO) token exchange, native to LibreChat 0.8.7 | Recommended over raw `addUserJwtToken` forwarding — exchanges for a token scoped to the MCP server's audience instead of passing the user's whole-session token to a downstream tool. Configured via `obo.scopes` on the MCP server entry in librechat.yaml. |

Prerequisites for OBO to work, confirmed against LibreChat 0.8.7 and Keycloak:
- LibreChat's OpenID connection to Keycloak must be configured for **reusable access tokens** (a LibreChat OpenID setting shipped alongside OBO in 0.8.7).
- Keycloak must support the underlying token exchange grant — **Standard Token Exchange (RFC 8693) has been officially supported since Keycloak 26.2** (previously a preview feature). **Confirmed:** the Keycloak version in use is above 26.2, so RFC 8693 support is not a blocker; token exchange still needs to be enabled on the client Keycloak uses for LibreChat (an admin-console step, not a version gap).
- Whoever administers LibreChat needs the `MCP_SERVERS.CONFIGURE_OBO` role permission to set `obo.scopes` on the RAG MCP server's config.

## 8. Open Questions

- Who owns keeping each user's `clearance`, `releasability`, and `groups` attributes current in Keycloak day to day — is there an authoritative source system to sync from, or is this manual admin-console maintenance? (Section 6.2 — Keycloak itself is no longer the open question, just the attribute-maintenance process.)
- Target end-to-end query latency budget (retrieval + rerank + generation) — compute headroom is confirmed (NFR-8) but no target has been set yet.
- Expected corpus size and ingestion rate (affects Qdrant sharding/collection strategy and multi-tenancy design).
- With Onyx Enterprise Edition ruled out by budget, and Dify identified as the strongest metadata/filtering primitive to build on: is extending Dify with a custom permission layer preferable to a fully custom ingestion UI, or is a clean-sheet build still preferred for consistency with how PING/MPNexus's other custom pieces were built?

## 9. Architecture Overview

See accompanying diagram. At a high level: an **Ingestion Web UI** (upload + mandatory classification tagging, with curator review) and **LibreChat** (end-user chat) are the two entry points. In the diagram, LibreChat traffic passes through the existing **LiteLLM gateway**; in practice, the query path is realized as a custom **MCP server** (Section 7.7) that LibreChat calls as an agent tool, with the user's Keycloak JWT forwarded to it — this MCP server *is* the "RAG orchestration pipeline" box, doing chunk → embed → retrieve → rerank and enforcing the claims-based access filter (Section 6.1) before ever touching Qdrant. It reads and writes the **Qdrant vector store** (payload-tagged with Classification/Releasability/Access-scope/Status) and calls the **Ollama/vLLM** inference layer for both embedding and generation.

## 10. References

- Vhavle, G. ["Building RAG Systems: From Zero to Hero."](https://dev.to/gautamvhavle/building-production-rag-systems-from-zero-to-hero-2f1i) Dec 2025.
- [Qdrant documentation](https://qdrant.tech/documentation/) — vector database, RBAC/JWT, multitenancy.
- [Qdrant 1.9 release notes](https://qdrant.tech/blog/qdrant-1.9.x/) — RBAC/JWT introduction.
- [RAGAS](https://github.com/explodinggradients/ragas) — RAG evaluation framework.
- [Onyx Access Controls documentation](https://docs.onyx.app/security/architecture/access_controls) — document-level RBAC is an Enterprise Edition feature.
- [Onyx Enterprise Edition documentation](https://docs.onyx.app/deployment/miscellaneous/enterprise_edition) — licensing/trial process.
- [AnythingLLM Security and Access documentation](https://docs.useanything.com/features/security-and-access) — workspace-level RBAC model.
- [Dify Metadata documentation](https://docs.dify.ai/en/use-dify/knowledge/metadata) and [Dify v1.1.0 metadata filtering announcement](https://dify.ai/blog/dify-v1-1-0-filtering-knowledge-retrieval-with-customized-metadata) — custom metadata as a retrieval filter.
- [LibreChat MCP Servers configuration documentation](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers) — `addUserJwtToken`, On-Behalf-Of token exchange, and per-user MCP OAuth.
- [LibreChat v0.8.7 release notes](https://www.librechat.ai/changelog/v0.8.7) and [PR #13429](https://github.com/danny-avila/LibreChat/pull/13429) — native OBO token exchange support for MCP server connections.
- [Keycloak: Standard Token Exchange officially supported in 26.2](https://www.keycloak.org/2025/05/standard-token-exchange-kc-26-2) — RFC 8693 compliance, prerequisite for LibreChat's OBO flow.
