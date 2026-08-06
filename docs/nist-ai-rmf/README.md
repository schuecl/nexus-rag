# NIST AI RMF documentation set

**Framework:** NIST AI Risk Management Framework (AI RMF 1.0) and its Playbook
**System:** MPNexus RAG pipeline (this repository, all four services + charts)
**Status:** Draft — organizational fields pending decision (issues #519–#524)
**Last updated:** 2026-08-06

## Why this directory exists

Everything under `docs/nist-ai-rmf/` is **compliance-facing documentation written in
the NIST AI RMF's vocabulary**, deliberately separated from the repository's
engineering documentation (`docs/*.md`, `REQUIREMENTS.md`, `ARCHITECTURE.md`). The
engineering docs are the *evidence*; these documents are the audit-facing index plus
the artifacts the RMF asks for that the repository did not previously have (impact
assessment, risk register, inventory, governance policy).

Rules for this directory:

- **Reference, don't duplicate.** Where an engineering doc already demonstrates an
  outcome, these documents point at it. Duplication drifts; pointers don't. If
  writing a document here reveals a contradiction with an engineering doc, the fix
  goes into the engineering doc (or an issue against it) — never a fork of the truth.
- **Honest confidence labeling** uses `docs/dev-setup.md`'s three levels
  (*implemented* / *tested against mocks* / *validated live*) plus two added
  locally for compliance work: *gap* and *TBD (organizational)*. Nothing here
  upgrades a claim past what was actually exercised.
- **`TBD (organizational)`** marks decisions only the deployment owner or
  accountable executive can make. Each such field names the issue tracking the
  decision. These are the blocking items before this set can be represented as an
  operating AI management system rather than a draft.

## AI management-system scope

The managed system is the RAG capability this repository delivers: document
ingestion with mandatory classification/releasability tagging, curator review,
claims-based access-controlled hybrid retrieval exposed as the `rag_search` MCP
tool, and the evaluation/monitoring machinery around them. The generation stack
downstream of the tool call (LibreChat → LiteLLM → vLLM/Ollama) is **outside this
system's boundary by design** (`REQUIREMENTS.md` constraint C7) — but risks that
cross that boundary (retrieved-content persistence in the chat plane, prompt
injection via retrieved chunks, output marking) are *in scope for risk tracking*
here even where their controls live with the platform. See
[risk-register.md](risk-register.md) rows R-1 and R-7 and `docs/governance.md`'s
DoD classified-information control profile for the boundary treatment.

## Document index

| Document | RMF function(s) | What it is |
|---|---|---|
| [governance-policy.md](governance-policy.md) | Govern | AI governance policy: accountable owner, risk tolerance, acceptable use, human oversight, incidents, change management, decommissioning |
| [rmf-mapping.md](rmf-mapping.md) | All four | Outcome-level map: RMF subcategory → repository artifact or honest gap |
| [ai-system-inventory.md](ai-system-inventory.md) | Govern 1.6, Map | System/component/model inventory with versions, origins, licenses; vendor and model-provider assessments; provenance and licensing register |
| [impact-assessment.md](impact-assessment.md) | Map | Impact assessment: intended/prohibited uses, affected users, foreseeable misuse, consequential-decision analysis — drafted from the system as built, ratified by answering its §7 owner questions |
| [risk-register.md](risk-register.md) | Map, Manage | AI risk register with corrective-action tracking |
| [evidence/evidence-index.md](evidence/evidence-index.md) | All four | Manifest of every evidence item: pointer, collection method, cadence, confidence |

## Open decisions ledger

Every question the doc set leaves for the deployment owner, in one place. Each is
also recorded in context in the named document; answering it there (with name and
date) is what closes it.

| # | Decision | Recorded in | Tracking |
|---|---|---|---|
| 1 | Accountable AI owner; platform-admin tier definition | [governance-policy.md](governance-policy.md) §2 | Issue #519 |
| 2 | Risk tolerance ratification + waiver authority | [governance-policy.md](governance-policy.md) §3 | Issue #521 |
| 3 | Retention periods; audit expiry yes/no; filename minimization | [governance-policy.md](governance-policy.md) §8 | Issue #520 |
| 4 | Incident response: receiver, response ladder, taxonomy ratification | [governance-policy.md](governance-policy.md) §7 | Issue #522 |
| 5 | Identity-governance mapping; classified-gate owners | [rmf-mapping.md](rmf-mapping.md) GOVERN 1.1 | Issue #523 |
| 6 | NFR-4 latency budget | [rmf-mapping.md](rmf-mapping.md) MAP 1.6 | Issue #430 |
| 7 | Golden-query gate: formal acceptance or required-via-path-filter | [risk-register.md](risk-register.md) R-6 | Issue #529 |
| 8 | qwen2.5 dev/judge defaults vs constraint C2: record exception or swap | [ai-system-inventory.md](ai-system-inventory.md) A1 | — |
| 9 | Milvus maintainer-of-record vs C2 before production use of that backend | [ai-system-inventory.md](ai-system-inventory.md) A3 | — |
| 10 | Corpus licensing field: accept scoping decision or add to metadata schema | [ai-system-inventory.md](ai-system-inventory.md) §6 | — |
| 11 | Impact-assessment ratification questions Q1–Q5 (consequential use, accreditation level, coalition users, PII posture, corpus scale) | [impact-assessment.md](impact-assessment.md) §7 | — |
| 12 | Management-review cadence + first review | [governance-policy.md](governance-policy.md) §9 | No issue yet |
| 13 | Curator risk-awareness training ownership | [governance-policy.md](governance-policy.md) §10 | No issue yet |

## Audit evidence package — where each item lives

| Evidence item | Location | Status |
|---|---|---|
| AI management-system scope | This README | Draft |
| RAG architecture and data-flow diagrams | [`ARCHITECTURE.md`](../../ARCHITECTURE.md) (component inventory §2, data model §3, sequence flows §4) + [`docs/architecture/diagrams.md`](../architecture/diagrams.md) | Implemented |
| AI system inventory | [ai-system-inventory.md](ai-system-inventory.md) | Draft |
| AI impact assessment | [impact-assessment.md](impact-assessment.md) | Draft — pending §7 owner answers |
| AI / infosec risk registers | [risk-register.md](risk-register.md) | Draft |
| Data provenance and licensing register | [ai-system-inventory.md](ai-system-inventory.md) §5–6 + [`docs/governance.md`](../governance.md) "Lineage and provenance" | Partial — corpus licensing is a gap |
| RAG evaluation methodology and test results | [`docs/testing.md`](../testing.md) (methodology); results: `.eval-history` trend store (CI), first archived snapshot pending (issue #532) | Methodology validated live; archived results pending |
| Security threat model and red-team reports | [`docs/threat-model.md`](../threat-model.md); red-team history in `REQUIREMENTS.md` §11 P1 (issues #97/#427/#457/#458); archived probe report pending (issues #494, #532) | Implemented; live re-validation pending |
| Privacy assessment and retention/deletion procedures | [`docs/governance.md`](../governance.md) ("Query confidentiality", "Retention and destruction"), [`docs/chat-plane-purge.md`](../chat-plane-purge.md), [`docs/threat-model.md`](../threat-model.md) | Implemented; retention schedule unratified (issue #520) |
| Human-oversight and acceptable-use procedures | [`docs/roles-and-permissions.md`](../roles-and-permissions.md) (oversight); [governance-policy.md](governance-policy.md) §5–6 (policy) | Oversight validated live; acceptable-use draft |
| Vendor and model-provider assessments | [ai-system-inventory.md](ai-system-inventory.md) §4 | Draft |
| Monitoring, incident and change-management records | [`docs/observability.md`](../observability.md), [`docs/siem-detection-runbook.md`](../siem-detection-runbook.md), [`docs/releasing.md`](../releasing.md), [`docs/credential-rotation.md`](../credential-rotation.md), GitHub Releases (SBOMs, digests, changelog) | Implemented; incident ownership pending (issue #522) |
| Internal audit and management-review evidence | [governance-policy.md](governance-policy.md) §9 defines the process | Gap — no review conducted yet; no tracking issue filed yet |
| Corrective-action register | [risk-register.md](risk-register.md) §3 | Draft |
