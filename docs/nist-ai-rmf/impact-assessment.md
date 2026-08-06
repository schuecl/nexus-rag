# AI impact assessment

**RMF outcomes served:** MAP 1/3/5.
**Status:** Draft — written from the system as built (requirements, threat model,
seeded deployment context), with the decisions that exceed this repository's
authority collected as explicit questions in §7. Answering §7 and recording who
answered converts this draft into the ratified assessment.

## 1. System context and intended use

The system serves MPNexus — an air-gapped Kubernetes environment operated for
USAREUR-AF — as its retrieval layer: authenticated users query organizational
documents (regulations, SOPs, technical manuals, product data sheets;
`REQUIREMENTS.md` §1) through LibreChat, whose agent calls the `rag_search` MCP
tool. The tool returns **cited, access-filtered evidence, not answers** (C7/FR-29);
the calling client's own model generates the answer. Ingestion is self-service for
authorized uploaders, but nothing becomes retrievable without human curator
approval (FR-11). Intended-use statement and prohibited uses are policy in
[governance-policy.md](governance-policy.md) §4; this assessment analyzes their
consequences.

## 2. Affected users and stakeholders

**Direct users** (roles are real, seeded, and enforced —
`docs/roles-and-permissions.md`):

- *Uploaders* (`rag-ingest`) — affected by rejection decisions (notified with
  reasons, FR-15) and by the audit trail binding submissions to their identity.
- *Curators* (`rag-curate:<org>`) — the humans making the consequential
  approval/classification decisions; they carry spillage responsibility, which is
  why their authority is capped by their own clearance (FR-14) and why training
  (governance-policy §10) matters most for this group.
- *Query users* (`rag-query`) — consumers of retrieved evidence; affected by both
  false negatives (missing evidence → decisions made without available
  information) and false positives in ranking (wrong document cited).
- *Administrators* (`rag-admin`, vocabulary only; `rag-purge`, destruction) —
  deliberately narrow; neither can read corpus content through their admin role.

**Indirectly affected parties:**

- *Document originators* — their content becomes retrievable to populations they
  may not have anticipated; the access-scope field and curator org-scoping are
  the controls; the curatorless-document blind spot (risk R-11) is the residual.
- *Individuals mentioned within corpus documents* — the privacy dimension: PII in
  regulations/rosters/manuals becomes queryable by anyone whose claims pass the
  filter. Controls: flag-only PII advisory at curation (deliberately advisory —
  `docs/governance.md` non-goals), query-confidentiality design (query text never
  stored), and the membership-inference analysis in `docs/threat-model.md`.
- *Coalition partners* — releasability tags (REL TO NATO/FVEY…) encode
  disclosure decisions; a mistag is a foreign-disclosure incident, not just a
  data-quality miss (incident class `spillage-via-mistag`, governance-policy §7).

## 3. Consequential-decision analysis

This is **decision support in a military operational context** — a
high-consequence category by any reading of the RMF, and this assessment says so
plainly rather than argue itself into a lower tier. The design mitigations that
keep the human decisive are structural, not aspirational:

1. The system returns evidence with source/classification citations (FR-21/FR-27)
   — a human can and must verify grounding before acting.
2. Everything retrievable passed a human curator; nothing enters the corpus
   ungated (FR-10..16).
3. On low confidence or an empty access-filtered result, the design says *say so*
   rather than let the model answer from memory (FR-28) — abstention behavior is
   measured (advisory) by the Q→C→A evaluator.
4. Generated answers are explicitly disqualified as derivative-classification
   sources until formal output marking exists (`docs/governance.md`
   output-handling rule).

What this repository cannot determine — whether outputs may inform personnel
actions, legal proceedings, or targeting-adjacent processes, and what added
oversight applies if so — is question Q1 in §7.

## 4. Foreseeable misuse

Grounded in the threat model's adversary taxonomy and the live red-team history,
with the existing control named for each:

| Misuse | Existing control | Residual |
|---|---|---|
| Insider reconnaissance: probing the access boundary, membership inference over the corpus | Recon-shaped query detection (4 signals, validated live) + SIEM runbook; hard filter bounds what probing can reach | Rank-order inference channel accepted (risk R-2); detector unscheduled (R-4) |
| Poisoning: injecting instructions or misleading content through the ingestion path | Mandatory curation; curator-facing injection-marker advisory; delimiter neutralization closed forgery structurally | Roleplay/citation-hijack classes mitigated by notice only; live re-validation pending (R-7) |
| Over-reliance: copying retrieved text verbatim into decisions without verification | Citations for verification; SECURITY_NOTICE instructs paraphrase-not-reproduce | Notice-only; a compliant-enough model or hurried human can still copy (R-7) |
| Spillage: uploading or approving content above authorized level | Claims-capped tagging (FR-18) + clearance-capped curation (FR-14), both server-side; purge + chat-plane procedure for cleanup | Chat-plane copies outlive purge (R-1) |
| Exfiltration by an authorized user of content they can legitimately read | Out of scope for access control by definition; audit trail + anomaly detection provide after-the-fact attribution | Detection-not-prevention — this assessment's own scoping, consistent with the threat model's detection-only recon posture |

## 5. Benefit / impact balance

The affirmative case is specific, not generic: in an air-gapped environment the
realistic alternative to governed RAG is users pasting documents into a chat
model directly — no classification filter, no audit trail, no curation, and
persistent copies in the chat plane *by default* rather than as a tracked
residual. This system replaces that with claims-enforced retrieval, mandatory
human review, and identity-keyed auditability of every access decision. The
residual-risk side of the balance is the [risk register](risk-register.md) —
notably R-1 (chat-plane persistence), R-7 (injection residuals), and the
monitoring-cadence cluster (R-4/R-5/R-13).

## 6. Assessment conclusion (draft)

High-consequence context, deliberately human-gated at both ends (curation before
retrievability, citation-verification before use), with residual risks that are
individually documented, bounded, and tracked. The assessment's validity depends
on the §7 answers — none of which change the system's design, but all of which
change what may be *claimed* about its operation.

## 7. Open questions for the deployment owner

Answers recorded here (with name and date) ratify this assessment.

- **Q1 (consequential use):** May outputs inform personnel, legal, or
  targeting-adjacent decisions? If yes, what additional human-review step applies
  beyond citation verification?
- **Q2 (accreditation level):** What is the deployment's system-high level, and
  is the corpus intended to include classified national security information? (If
  yes, the classified-deployment authorization gate in `docs/governance.md`
  becomes blocking — issue #523.)
- **Q3 (affected-population scope):** Are coalition-partner users (non-US
  personnel with releasability-scoped access) in the user population, or only US
  personnel with coalition-releasable *content*?
- **Q4 (PII posture):** Is the flag-only PII advisory sufficient for the intended
  corpus, or does any document class require redaction-before-ingest as policy
  (kept outside this system, per its non-goals)?
- **Q5 (corpus scale):** Expected corpus size and ingestion rate
  (`REQUIREMENTS.md` §8 open question) — affects whether the membership-inference
  and quota risks (R-2, R-9) stay at their current Medium-likelihood ratings
  rather than escalating.
