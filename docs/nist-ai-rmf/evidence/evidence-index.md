# Evidence index

Manifest of every evidence item in the audit package: where it lives, how it is
produced, and how often it refreshes (confidence labels live with each claim in
the documents themselves). Rules:
**pointers for anything regenerable; snapshots only for run results** that
constitute point-in-time evidence. Evidence produced with content-bearing flags
(`--include-content`) inherits the corpus handling level per `docs/governance.md`'s
evaluation-handling rule and must **not** be committed here — only content-free
default reports belong in this repository.

## Standing evidence (pointers — regenerated continuously)

| Evidence | Pointer | Produced by | Cadence |
|---|---|---|---|
| Required-gate enforcement | Branch-protection rulesets on `main`; `docs/testing.md` "Branch protection" | GitHub rulesets | Continuous |
| Unit/BDD/coverage results | `ci.yml` runs (unit, service-tests) | Every PR/push | Continuous |
| Static/security analysis | `security.yml` (bandit, pip-audit, trivy-fs, gitleaks), `codeql.yml` | Every PR + weekly | Continuous |
| Mutation kill-rate | `e2e.yml` `mutation` job; baseline in `docs/testing.md` | Nightly | Continuous |
| Retrieval-quality trend | `.eval-history` store, carried across CI runs via artifacts | Nightly and manual non-PR runs write the store; labeled PR runs evaluate but never write it | Continuous |
| Release provenance (SBOMs, digests, changelog) | GitHub Releases (immutable tags) | Per release | Per release |
| Audit-log append-only proof | `tests/integration/` against live Postgres grants | Nightly + labeled PRs + manual | Continuous (nightly) |
| Live-privilege verification | `docs/roles-and-permissions.md` §6 matrix + §7 G2 (each forbidden operation and escalation path attempted live) | Manual, documented | Re-verify on auth changes |

## Snapshot evidence (`snapshots/<YYYY-MM-DD>/` — captured per audit period)

First snapshot: pending (issue #532). Each snapshot directory must contain a
`run-metadata.md` (date, stack version, corpus manifest hash, operator) — that
metadata is what makes a file evidence rather than an artifact.

| Item | Producer | Notes |
|---|---|---|
| `injection-probe/` | `scripts/adversarial_injection_probe.py` | Red-team evidence; the live-validation debt itself was closed by PR 541's run (all 5 cases) — this snapshot archives such a run as evidence at rest |
| `access-matrix/` | `scripts/verify_corpus_access.py` | Multi-persona FR-26 scoping evidence |
| `rag-quality/` | `scripts/evaluate_rag_quality.py` (default content-free mode) | Q→C→A judged report with config fingerprint |
| `retrieval-eval/` | copy of the period's `.eval-history` reports | FR-30 trend evidence at rest |
| `anomaly-detection/` | `scripts/detect_query_anomalies.py` | Recon-detection run evidence |
| `mutation/` | mutmut tally + `check_mutation_score.py` output | Kill-rate evidence at rest |
| `ci-status.md` | manual capture | Required-checks list + green run links at snapshot time |
| `management-review.md` | management review (governance-policy §9) | Minutes; **first review pending**. Template and directory layout now exist ([snapshots/](snapshots/)) |
| `internal-audit.md` | `scripts/audit_rmf_mapping.py --report`, completed by hand | Mechanical findings + diff since the last accepted `baseline.json`, then the auditor's judgement and signatures; **first audit pending** |
| `incidents/` | incident records per governance-policy §7's taxonomy | **Zero incidents recorded to date** — an empty directory in a snapshot is itself the record |

## Known evidence gaps (honest list)

- No snapshot has been captured yet (issue #532).
- No internal audit or management review has been conducted (governance-policy
  §9; tracked in issue #542). The mechanism now exists -- audit method
  (`scripts/audit_rmf_mapping.py`), evidence layout, `baseline.json`, and a
  minutes template -- so what is missing is a review being *held*: a cadence
  ratified by a named decider, and the first set of signed minutes. Neither can
  be produced retroactively, which is why this remains a gap rather than a draft
  like the other thirteen items.
- The SIEM detection sketches have never been executed against a production SIEM
  (`docs/siem-detection-runbook.md` is documentation-only; issue #522).
- `helm/observability` and the ServiceMonitor path have never run on a real
  cluster (`docs/observability.md` states this plainly).
