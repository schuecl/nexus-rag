# AI governance policy

**RMF outcomes served:** GOVERN (all), MANAGE 1/4.
**Status:** Draft. Sections marked **TBD (organizational)** are decisions only the
deployment owner can make; each names its tracking issue. Everything else states
policy that the repository already enforces mechanically — written down so an
auditor can see the policy, not just the mechanism.

## 1. Purpose and scope

This policy governs the MPNexus RAG pipeline (scope defined in
[README.md](README.md)). It complements, and never overrides, the engineering
documentation: `docs/governance.md` remains authoritative for data governance
(access control, curation, lineage, retention design); `docs/testing.md` for what
is gated vs advisory; `docs/roles-and-permissions.md` for the authorization
matrix. Where this policy and an engineering doc disagree, the engineering doc is
the bug *or* this policy is — file an issue; do not fork the truth.

## 2. Accountable ownership — TBD (organizational), issue #519

- Accountable AI owner: **unnamed**. Risk acceptance, gate waivers, and incident
  escalation have no defined terminus today.
- Platform-admin tier (holders of `POSTGRES_USER`, object-store root, Keycloak
  admin, Qdrant keys): **undefined** — flagged in `docs/governance.md`'s roles
  table as the only access the application does not mediate.
- Until issue #519 closes, `REQUIREMENTS.md`'s document owner ("Corey / MPNexus
  platform") is the de-facto escalation point; this sentence is a statement of
  current fact, not a ratified assignment.

## 3. Risk tolerance — proposed, pending ratification (issue #521)

Proposed framing, consistent with what the code already enforces:

| Risk class | Tolerance | Enforcement point |
|---|---|---|
| Access-control regression (forbidden-status or out-of-scope content reaching a user) | **Zero.** Any FR-26 leak is a security incident, not a quality miss | Golden-harness hard-fail (nightly + labeled PRs only — see risk R-6 / issue #529); BDD scenarios on every PR; mutation gate ≥80% on `claims.py`/`qdrant_filters.py`/`metadata.py`/`versioning.py` |
| Retrieval-quality regression | **Bounded**: within `--regression-tolerance` of baseline | `evaluate_retrieval.py` fail-closed defaults |
| Judged answer-quality metrics | **Advisory**: comparative only, same judge/prompt/golden-set | `relative_only_same_judge_and_prompt` stamped in every report |
| Availability/latency | Undefined until NFR-4 budget agreed (issue #430) | Provisional 5s p95 alert |

**Waiver authority: TBD.** Ruleset admin-bypass exists with no policy around it.
Proposed: waivers only by the accountable owner, recorded as a PR comment naming
the gate, reason, and expiry.

## 4. Acceptable use

**Intended use.** Grounded retrieval over curator-approved organizational
documents for authenticated, authorized users, with source/classification
citations returned for verification (FR-21/FR-27). The system returns *evidence*;
answer generation belongs to the calling client (C7).

**Prohibited uses** (drawn from limits the engineering docs already state):

1. Outputs must not be used as a source for **derivative classification** —
   generated answers lack banner/portion/authority markings
   (`docs/governance.md`, output-handling rule).
2. Retrieval results must not be treated as a **completeness claim** — absence of
   results is not evidence of absence (access scoping means different users see
   different corpora by design).
3. No user may attempt to access content beyond their clearance/releasability/
   access-scope. Reconnaissance-shaped query patterns (volume, denial ratio,
   narrow-result probing) are detectable when the offline detector or adapted
   SIEM rules run (`docs/siem-detection-runbook.md`); filter-edge probing
   specifically is not observable (out-of-scope queries return successful
   empty results — threat model §4), and continuous detection is the open
   R-4/R-15 gap. Flagged identities are treated per §7.
4. The advisory scans (classification suggestion, PII, marking detection) must
   never be treated as decisions — they are curator-facing hints by design.
5. Uploading content above the environment's accreditation level is spillage,
   handled per §7, not a tagging correction.

## 5. Human oversight

Oversight is concentrated where consequence is highest, and is mandatory, not
optional:

- **Every document** passes a human curator (org-scoped, clearance-capped, FR-10..16)
  before becoming retrievable — validated live. Curators can also suspend
  approved documents (issue #478) — implemented, tested against mocks.
- **Destruction** supports two-person control (`PURGE_TWO_PERSON_REQUIRED`) —
  implemented, tested against mocks; the confirm step is API-only today (gap
  G3, no UI). Destruction evidence is retained by design.
- **Output-side oversight is deliberately out of scope** (C7): generation happens
  in the calling client. The compensating controls are citations for human
  verification (FR-27) and abstention on low-confidence/no-result (FR-28). This
  is a recorded scoping decision, revisited if the system boundary ever changes.

## 6. Model and system change management

Policy: no unpinned artifact reaches production (NFR-16, CI-enforced); any change
to embedding model, chunking, or reranker **mandates re-evaluation** against the
golden set (`docs/testing.md` re-evaluation policy); version lockstep and
immutable releases make every prior version redeployable (release publish flow
validated live at v0.1.0; rollback-by-redeploy is implied by immutability, not
separately exercised). Gap being closed: the retrieval-eval
config fingerprint that makes cross-change comparisons impossible to miss
(issue #525).

## 7. Incident response — TBD (organizational), issue #522

Proposed AI-incident taxonomy (to be ratified with the response ladder):

| Class | Example | Proposed severity |
|---|---|---|
| `access-control-leak` | Non-approved or out-of-scope content in results | Critical — zero-tolerance class of §3 |
| `spillage-via-mistag` | Over-classified content approved under wrong tags | Critical; invokes purge + `chat_plane_action_required` procedure |
| `injection-compromise` | Generation observably follows corpus-embedded instructions | High; see risk R-7 |
| `quality-regression` | Baseline gate failure | Medium |
| `detector-stale` | Watchdog alerts (anomaly-detector staleness today; quality staleness once issue #526 lands) | Medium — monitoring itself failed |

Blocking prerequisites, both tracked in issue #522: a real Alertmanager receiver
(default is a no-op) and a decided response ladder for flagged identities.

## 8. Decommissioning and data disposition

Mechanisms: supersession without a dead window (NFR-13), purge with tombstones
and retained destruction evidence, chat-plane purge procedure. Policy blocked on
the retention ratification (issue #520) — until then the implemented subset
(purge, session reaping) is the only destruction that happens, and that fact is
itself the recorded policy.

## 9. Internal audit and management review — TBD (organizational)

Proposed (no tracking issue filed yet — the one evidence-package item with none):

- **Management review**: quarterly; inputs are this policy's TBD ledger, the
  [risk register](risk-register.md), the evidence index, and the trend stores;
  minutes archived under `evidence/snapshots/<date>/management-review.md`.
- **Internal audit**: annually; method = re-run the assessment behind
  [rmf-mapping.md](rmf-mapping.md) against current `main` and diff the statuses.
- The first conducted review converts this section from proposal to record.

## 10. Training and awareness — TBD (organizational)

Role-facing how-to articles exist in-app (FR-33, claims-gated). What the RMF asks
beyond that — periodic risk-awareness training for curators (the humans making
consequential approval decisions) and evidence it happened — does not exist and
is owned by the deployment, not this repository. Recorded as a gap.
