# NIST AI RMF mapping — outcome by outcome

How this system demonstrates (or does not yet demonstrate) each NIST AI RMF 1.0
outcome. Subcategories are grouped where one artifact serves several. Statuses use
the repo convention: **Validated live** / **Tested against mocks** / **Implemented**
(code/docs exist, not exercised here) / **Gap** / **TBD (organizational)** —
plus **Partial** (some subcategories in the group met, others not) and
**Draft** (artifact written, ratification pending).

Assessment date: 2026-08-06, against `main` at the commit this file was introduced.
Three open PRs (rank-aware metrics + config fingerprint; golden-set expansion;
latency benchmark) will improve several MEASURE rows when merged — rows note this
rather than pre-claiming it.

## GOVERN

| Outcome | Evidence / gap | Status |
|---|---|---|
| 1.1 Legal/regulatory requirements understood and documented | DoD marking practice drives the metadata schema (`REQUIREMENTS.md` §6.3, CAPCO-style); DISA STIG expectations named in NFR-2; adapted-control mapping with per-control residuals in `docs/governance.md` "DoD classified-information control profile". Residual: application roles are not evidence of clearance adjudication — issue #523 | Implemented; execution TBD (organizational) |
| 1.2 Trustworthy-AI characteristics in policy | Enforced by construction (server-side claims, mandatory curation, append-only audit) rather than stated as policy; [governance-policy.md](governance-policy.md) is the policy artifact | Draft |
| 1.3 Risk tolerance defined; activities calibrated to it | Declared per risk class in [governance-policy.md](governance-policy.md) §3.1, with release-acceptance criteria (§3.2), waiver authority/record (§3.3, [waiver register](evidence/waiver-register.md)), and the R-6 standing acceptance (§3.4). Issue #521 | Implemented (ratification pending owner, issue #519) |
| 1.4 Risk processes transparent and documented | `docs/testing.md`'s gated-vs-advisory list; `docs/governance.md` practice map with three-level status convention | Validated live (convention actively maintained) |
| 1.5 Monitoring & periodic review planned | Prometheus rules + dashboards (`docs/observability.md`); nightly golden-query + mutation runs; **but** the alert receiver is a no-op default, no alert rule consumes the published quality gauges, and nothing runs their producer on a schedule — issues #522, #526, #527. Periodic review of the risk-management *process* (as distinct from the system): method, evidence layout and minutes template now exist (governance-policy §9, `scripts/audit_rmf_mapping.py`), **cadence unratified and no review yet conducted** — issue #542 | Partial |
| 1.6 AI system inventory | [ai-system-inventory.md](ai-system-inventory.md), mechanically anchored to the pin checks (`scripts/check_pinned_images.py`, `check_pinned_models.py`) | Draft |
| 1.7 Safe decommissioning | Supersession (FR-7/NFR-13), purge with tombstones, retention design — unratified; issue #520 | Implemented; ratification TBD (organizational) |
| 2.1–2.3 Roles, responsibilities, executive accountability | Application roles implemented and covered by BDD/mocks, with retrieval-side enforcement and machine-identity least privilege validated live (`docs/roles-and-permissions.md`); governance-role table in `docs/governance.md`; accountable-owner role and platform-admin tier now **defined** ([governance-policy.md](governance-policy.md) §2 — credential inventory, break-glass rules, statement-logging evidence trail); the **names** (owner appointment, credential holders) remain the deployment owner's — issue #519 | Implemented (role definitions); appointment TBD (organizational) |
| 3.1 Workforce diversity/capacity | Single-maintainer repository; team composition is a deployment-owner matter | TBD (organizational) |
| 3.2 Human oversight roles | Mandatory curation gate (FR-10..16) with org-scoped curator authority — validated live; two-person purge and curator suspension (#478) — implemented, tested against mocks (purge confirm step is API-only, no UI: gap G3). Oversight of *outputs* is out of scope by design (C7) — recorded in [governance-policy.md](governance-policy.md) §5 | Partial (labels per mechanism) |
| 4.1–4.3 Safety-first culture, risk communication | Honest-labeling convention is the repo's working culture; `docs/testing.md` documents what is *not* covered. `sec:*`/`severity:*`/`tool:*` GitHub label taxonomy for tracking security findings; private vulnerability reporting via `SECURITY.md` (the issue-template config deliberately routes security reports away from public issues) | Implemented |
| 5.1–5.2 External feedback mechanisms | Uploader notification on curation decisions (FR-15); in-app knowledge base (FR-33); no external-stakeholder feedback channel beyond the issue tracker | Partial |
| 6.1–6.2 Third-party risk policies | Origin/license screening (C1/C2) applied to every candidate (`REQUIREMENTS.md` §7); pinned images/models (NFR-16); pip-audit + trivy-fs blocking on PRs; SBOMs per release. Vendor assessment doc: [ai-system-inventory.md](ai-system-inventory.md) §4 | Validated live (mechanisms); assessments draft |

## MAP

| Outcome | Evidence / gap | Status |
|---|---|---|
| 1.1–1.2 Context, purposes, assumptions established | `REQUIREMENTS.md` §1–3 (purpose, background, constraints C1–C9); `ARCHITECTURE.md` §1 system overview | Implemented |
| 1.3–1.4 Organizational goals, business value | `REQUIREMENTS.md` §1–2 (grounded answers over org documents; self-hosted rationale) | Implemented |
| 1.5 Organizational risk tolerance mapped | See GOVERN 1.3 — [governance-policy.md](governance-policy.md) §3.1 (issue #521) | Implemented (ratification pending owner, issue #519) |
| 1.6 System requirements incl. performance targets | FR-1..FR-34, NFR-1..NFR-18; **NFR-4 retrieval+rerank budget agreed (issue #430), full end-to-end incl. generation still open** — issue #573 | Implemented; NFR-4 partial |
| 2.1–2.3 Tasks, methods, data flows documented | `ARCHITECTURE.md` §2 component inventory, §3 data model, §4 sequence flows (ingestion, curation, retrieval, login, supersession, tagging advisory) | Implemented |
| 3.1–3.5 Benefits, costs, misuse, oversight capacity | Misuse: `docs/threat-model.md` adversary taxonomy + its four attack-surface sections (the fourth is detection-oriented); oversight capacity: curator queue scoping. consolidated impact assessment drafted ([impact-assessment.md](impact-assessment.md)); ratification of its §7 owner questions pending | Partial; assessment Draft — ratification TBD (organizational) |
| 4.1–4.2 Third-party risks mapped | C2 origin screening with named exclusions; `REQUIREMENTS.md` §7 tables; residual: dev generation/judge model origins — [ai-system-inventory.md](ai-system-inventory.md) §4 | Implemented; one open rationale |
| 5.1–5.2 Impacts to individuals/society characterized | Privacy threat model (`docs/threat-model.md`: membership inference, retrieved-content leakage, recon patterns); query-confidentiality analysis in `docs/governance.md` | Implemented |

## MEASURE

| Outcome | Evidence / gap | Status |
|---|---|---|
| 1.1–1.2 Metrics selected, suitability assessed | Retrieval: recall@K/precision@K/first-relevant-rank (`scripts/evaluate_retrieval.py`); Q→C→A: 7 judged metrics incl. faithfulness + deterministic citation validity (`scripts/evaluate_rag_quality.py`), abstention deliberately diagnostic-only (judge-reliability finding recorded) | Validated live |
| 1.3 Assessment by independent parties | No assessor independent of the developer exists. The internal audit is the closest mechanism and its **method is now recorded and executable** (`scripts/audit_rmf_mapping.py`, governance-policy §9.1: reference integrity, status vocabulary, referenced-issue state, and a diff against the last accepted snapshot — no network, no authorship of the documents required, which is what makes a non-author performer viable). What remains is not tooling but a person: **who** performs it, and on what independence basis, is an owner decision with the options and their tradeoffs tabulated in §9.3 — issue #542. The generated report is explicitly not a completed audit until its judgement section and signatures are filled | Gap (performer TBD (organizational); method recorded) |
| 2.1 Test sets representative | Golden set: 8 queries, 1 persona, single-doc expectations; 4/8 saturated lexically — issue #528 (partially pre-empted by open golden-set PR) | Gap (known, tracked) |
| 2.2 Human-subject evaluations | Not applicable — no human-subject data collection | N/A (documented) |
| 2.3 TEVV for the system as deployed | CI gate chain (`docs/testing.md`) covers the build; deployed-conditions TEVV shares the 2.4/2.5 gap — issues #526/#527 | Partial |
| 2.4 Deployed-system performance measured | **Nothing measures a deployed environment** — nightly e2e measures an ephemeral CI stack. Issues #526/#527 | Gap |
| 2.5 Evaluations representative of deployment conditions | Single-persona eval; multi-persona machinery exists (`scripts/verify_corpus_access.py`, advisory) — issue #528 | Partial |
| 2.6 Performance vs defined targets | FR-26 leak check: hard-fail, zero-tolerance (validated live). Latency: retrieval+rerank has an agreed target (NFR-4/issue #430); full end-to-end incl. generation does not | Partial |
| 2.7 Security & resilience (red-team) | Live prompt-injection probe with dated findings and fix history (`REQUIREMENTS.md` §11 P1: #97 mixed result → #427 notice fix → #457/#458 delimiter-forgery closed structurally / citation-hijack notice-only); adversarial-input NFR-7 tests; **strengthened wording not yet re-validated against live generation** — issues #494, #532. Also PyRIT finding open (#488) | Partial — validation debt recorded |
| 2.8 Transparency/accountability measured | Append-only audit log proven against live grants (integration layer) — validated live; trace_id correlation on sampled query rows only (tracing opt-in, 5% default sampling, 100% in the dev/CI stack; omitted when disabled) | Validated live (append-only); Partial (trace coverage) |
| 2.9–2.11 Model explanation, privacy, fairness probing | Privacy: content-free metrics allowlist (#127), query text never stored (#125), canary-phrase attribution; fairness/bias probing: not performed — corpus is regulations/SOPs, personas differ by *authorization*, not protected class; this row is itself the record of that scoping decision, ratified with the rest of the doc set (README ledger) | Privacy validated live; fairness scoped out (documented) |
| 2.12 Environmental impact | Self-hosted on existing GPUs (NFR-8); not otherwise tracked | Scoped out (documented) |
| 2.13 Measurement effectiveness evaluated | Judged-metric reliability finding (#386) led to abstention being de-gated — exactly this outcome; mutation testing measures the *tests* (≥80% kill gate, baseline 88%); retrieval baseline can silently span config changes — issue #525 (open PR adds the fingerprint) | Partial |
| 3.1–3.3 Risk tracking over time | `.eval-history` trend store carried across CI runs; rolling nightly baseline (release-pinned baseline still open, issue #429 second half); anomaly detector has staleness watchdog; **offline jobs unscheduled** — issue #527 | Partial |
| 4.1–4.3 Measurement feedback loops | Regression gates fail closed by default; curation-advisory calibration reports per-suggester agreement (`scripts/calibrate_tagging_advisory.py`, advisory) | Implemented |

## MANAGE

| Outcome | Evidence / gap | Status |
|---|---|---|
| 1.1 Deployment decision vs documented criteria | Gate chain exists; acceptance policy + waiver authority not written — issue #521 | TBD (organizational) |
| 1.2–1.4 Risk treatment prioritized, documented | Accepted risks are written decisions with issue numbers (chat-plane #286, rank-order residual, label-gated e2e); consolidated in [risk-register.md](risk-register.md) | Draft (register); decisions implemented |
| 2.1 Resources allocated to mapped risks | Highest-consequence modules get the strictest gate (mutation testing scoped to `claims.py`/`qdrant_filters.py`/`metadata.py`/`versioning.py`) — risk-proportionate by construction | Validated live |
| 2.2–2.3 Value sustained through change | Pinned images/models + version lockstep + config-fingerprinted quality eval; retrieval-eval fingerprint pending (issue #525) | Validated live / one gap |
| 2.4 Mechanisms to supersede/disengage | Supersession with no-dead-window property (NFR-13); curator suspension (#478); purge; rollback-by-redeploy — a design property of immutable, never-overwritten tags (release publish flow validated live at v0.1.0; a rollback itself has not been exercised) | Validated live (supersession/purge); Implemented (rollback) |
| 3.1–3.2 Third-party risks managed | Weekly trivy-image + SBOM (non-PR), blocking pip-audit/trivy-fs on PR; third-party images mirrored by documented retag pattern | Implemented |
| 4.1 Post-deployment monitoring | Alert rules + dashboards exist; receiver no-op, no alert consumes the quality gauges and nothing schedules their producer, offline jobs unscheduled — issues #522/#526/#527 | Gap (substrate exists) |
| 4.2 Measurable improvement activities | Issue-driven, FR/NFR-traceable changes; golden-set/fingerprint/latency PRs open | Validated live (process) |
| 4.3 Incident response | SIEM runbook + detections + `chat_plane_action_required` signal; **response ladder and receiver are deployment decisions not yet made** — issue #522 | Partial |

## Reading this honestly

The pattern across every Gap/TBD row is the same: *capability exists; policy,
named human, or scheduler missing*. No row above claims enforcement that does not
exist, and several rows record deliberate scope-outs (fairness probing,
environmental tracking, output-side oversight) as documented decisions — the RMF
treats a recorded, reasoned scope-out as compliant; an unstated one as a finding.
