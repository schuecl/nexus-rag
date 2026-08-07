# AI risk register and corrective-action tracking

**RMF outcomes served:** MAP 3/5 (risks characterized), MANAGE 1–4 (treatment,
tracking, response).
**Convention:** every row cites where the risk was originally analyzed — most were
already written decisions in engineering docs before this register existed; the
register consolidates, it does not invent. Likelihood/impact are qualitative
(L/M/H) pending the risk-scale decision in issue #521. Owner is TBD
(organizational) on every row until issue #519 names one; the *tracking* reference
is the accountable artifact meanwhile.

## 1. Register

| ID | Risk | L | I | Current controls | Treatment | Tracking |
|---|---|---|---|---|---|---|
| R-1 | Retrieved chunk text persists in LibreChat's Mongo store / LiteLLM logs beyond purge's reach, without this system's controls attached | H | M | `chat_plane_action_required` audit signal on purge; operator procedure (`docs/chat-plane-purge.md`) | **Accepted** (decided, issue #286) | `docs/threat-model.md` open items; review at each management review |
| R-2 | Rank-order residual membership-inference channel (authorized user infers corpus membership from result ordering) | M | L | Hard access filter bounds what can be inferred to documents the user can already see signals of; recon detection (R-4's detector) observes probing patterns | **Accepted, unmitigated** (documented) | `docs/threat-model.md` |
| R-3 | Qdrant payload cleartext at rest (chunk text + access-control fields readable if storage layer compromised) | L | H | Collection split, NetworkPolicy, RO/RW key separation; encryption at rest is a deployment storage-layer property (NFR-6, PyKMIP candidate) | **Bounded, deployment-dependent** | `docs/threat-model.md`; deployment checklist |
| R-4 | Offline detection jobs (query-anomaly detector, tagging calibration) have no scheduler — continuous-monitoring signal is operator-cadence | H | M | Staleness watchdog fires if the detector stops after having run at least once; it cannot notice a detector that has never run (absent metric, no series) | **Mitigating** | Issue #527 |
| R-5 | Alertmanager default receiver is a no-op: all 12 alert rules page nobody | H | H | Alerts visible in Alertmanager/Grafana for an operator who looks | **Mitigating** | Issue #522 |
| R-6 | Golden-query e2e (only leak/quality gate against a live stack) is label-opt-in; most PRs merge without it | M | M | Nightly run + carried trend store; mutation gate on filter modules; BDD scenarios on every PR | **Accepted** — formally recorded with conditions in [governance-policy.md](governance-policy.md) §3.4 | Issue #529 |
| R-7 | Prompt injection via curated corpus content into downstream generation. Live-probed history: direct override **resisted** (both probed models), exfiltration **resisted** (3B; 7B not recorded); DAN/roleplay **partially complied** (marker echo) → notice strengthened (#427); delimiter forgery **closed structurally** (#458); citation hijack mitigated by notice only (#457). Both #457/#458 fixes **validated against live generation** (PR 541: neither injected canary reached a final answer across all 5 fixture cases); the DAN/roleplay marker-echo residual was reconfirmed open | M | H | Delimiter neutralization (deterministic, unit-tested); SECURITY_NOTICE on both surfaces; curator-facing injection-marker advisory scan; access filter bounds blast radius to content the *querying user* is cleared for | **Mitigating** — residual is the roleplay marker echo and the notice-only posture of the citation-hijack mitigation | Issue #532 (archive the run as snapshot evidence); `REQUIREMENTS.md` §11 P1 |
| R-8 | Platform-admin tier unmediated: `POSTGRES_USER` superuser and store-level credentials bypass every application control incl. audit append-only | M | H | Credential separation (NFR-3), per-service least-privilege roles verified live; role now defined on paper with break-glass-only permitted actions and register entries per use ([governance-policy.md](governance-policy.md) §2.2); superuser sessions statement-logged at the DB layer (detective only — resettable by the superuser itself) | **Mitigating** — residual is the tamper-proofness limit (platform/personnel boundary) and the unfilled holder assignment | Issue #519 (owner naming); G2 residual in `docs/roles-and-permissions.md` |
| R-9 | No per-identity upload quota: one `rag-ingest` user can submit unboundedly many individually-compliant uploads | M | M | Single-request size cap (NFR-7); reap-on-use housekeeping; edge rate-limiting is platform responsibility (NFR-17) — quota explicitly has no owner | **Open, recorded in NFR-17** | `REQUIREMENTS.md` NFR-17 |
| R-10 | Retention schedule unratified → growth outside the purge path is monotonic (audit log, notifications, rejected/superseded artifacts) | H | L→M (grows) | Purge + session reaping (the implemented subset); audited-expiry design complete but deliberately unimplemented | **Open — decision, then scoped work** | Issue #520 |
| R-11 | Curatorless-document blind spot: a document whose owning org has no curator matching its access_scope sits in `pending_review` with no reviewer and no proactive warning | L | M | Discoverable only by the document's status remaining `pending_review` — no curator queue or admin surface shows it; accepted consequence of the G1 fix (#277) | **Accepted** | `docs/roles-and-permissions.md` G1 |
| R-12 | Retrieval-eval baseline comparisons can silently span an embedding/reranker config change | M | M | Re-evaluation policy documented in `docs/testing.md`; quality evaluator already fingerprints and refuses | **Mitigating** — open PR adds the fingerprint; refuse-on-mismatch behavior to verify at merge | Issue #525 |
| R-13 | No production quality-drift monitoring: quality gauges published and dashboarded, but no alert rule consumes them and nothing runs the evaluator unattended | H | M | Nightly CI eval (measures the build, not a deployment); reranker-fallback alert as coarse proxy | **Open** | Issue #526 |
| R-14 | PyRIT red-team finding: multi-turn system-prompt extraction objective reached | M | M | Live-validated via a 3-turn escalating extraction attempt against real generation (PR 541): no system prompt or SECURITY_NOTICE text leaked | **Closed — false positive** (issue #488, closed with evidence) |
| R-15 | SIEM detection content is documentation-only: sketches untested by CI, thresholds untuned, response ladder undefined | M | M | In-repo equivalent detector validated live provides cross-check | **Open** | Issue #522; `docs/siem-detection-runbook.md` |

## 2. Reading the register

Accepted rows (R-1, R-2, R-6, R-11) are not weaknesses of the register — each is a
documented, reasoned decision with a written source, which is precisely what MANAGE
1.3 asks for. The register's real debt is concentrated where every assessment of
this system keeps landing: R-4/R-5/R-13 are one theme (monitoring substrate without
a human on the end) and R-8/R-10 are the second (decisions above the application's
authority, unmade).

## 3. Corrective-action tracking

Corrective actions are tracked as upstream issues — the register does not duplicate
their state; check the issue for current status.

| Action | Addresses | Reference |
|---|---|---|
| Name accountable owner; define platform-admin role | R-8 | Issue #519 |
| Ratify retention; implement expiry per existing design | R-10 | Issue #520 |
| Risk-tolerance + acceptance policy (incl. risk scale for this register) | register-wide | Issue #521 |
| Wire alert receiver; response ladder; incident taxonomy; tune SIEM thresholds | R-5, R-15 | Issue #522 |
| Identity-governance mapping; classified-gate owners | R-8 adjacent | Issue #523 |
| Config fingerprint in retrieval eval | R-12 | Issue #525 (+ open PR) |
| Unattended evaluator + quality alert rules | R-13 | Issue #526 |
| Schedulers for offline jobs | R-4 | Issue #527 |
| Golden-set expansion (personas, adversarial, multi-doc) | R-6 adjacent | Issue #528 (+ open PR) |
| Golden-gate formal risk acceptance or path-filter requirement | R-6 | Issue #529 |
| Injection-probe live re-run with forgery/hijack fixtures — **done** (PR 541); archive the report as snapshot evidence | R-7 | Issue #532 |
| Triage PyRIT extraction finding — **done**: false positive, closed with live evidence (PR 541) | R-14 | Issue #488 |

Review cadence for this register: at each management review
([governance-policy.md](governance-policy.md) §9) and on any new accepted-risk
decision.
