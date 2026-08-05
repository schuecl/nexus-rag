# Testing & CI Quality Gates

How this repo verifies itself: the local/CI test pyramid, what each GitHub
Actions workflow enforces, and the honest current state of each gate. Related:
[docs/dev-setup.md](dev-setup.md) for the live-stack walkthrough, and
[scripts/evaluate_retrieval.py](../scripts/evaluate_retrieval.py) for the
golden-query harness the e2e workflow reuses, and
[scripts/evaluate_rag_quality.py](../scripts/evaluate_rag_quality.py) for the
manual local-judge Q→C→A evaluation.

## The pyramid

| Layer | Where | Runs in | What it pins |
|---|---|---|---|
| Unit | `tests/unit/common/` | `ci.yml` (every PR, py 3.11 + 3.12) | Claims parsing & signature verification, the FR-26 access filter, FR-18 metadata enforcement, classification ranking, FR-7 supersede guards, object store (incl. path-traversal), job-queue publishing |
| Unit | `services/*/tests/` | `ci.yml` job `service-tests` (one invocation per service) | FR-4 chunking (boundaries/overlap, atomic tables, oversized chunks), FR-3/NFR-7 parsing (incl. zip-bomb guard, table extraction), FR-25 reranking (incl. degraded-mode fallback), plus the #106-#109 regression guards |
| BDD security scenarios | `tests/e2e/features/access_control.feature` | `ci.yml` (in-process, no stack needed) | The Section 6 invariants as readable Gherkin: approved-only status, clearance ceiling, releasability holdings, cross-org isolation, curator scoping, supersede guards |
| Containerized integration (issue #428) | `tests/integration/` | `e2e.yml` job `integration` (same `needs-e2e` gating as golden-query) | NFR-2 append-only audit-log enforcement, verified against a real Postgres role/grant, not a mock -- see "Containerized integration layer" below |
| Retrieval quality + leak check | `scripts/evaluate_retrieval.py` + `golden_queries.json` | `e2e.yml` (nightly, manual, or a PR labeled `needs-e2e`) | Full `docker compose up` → seed → golden-query run; fails on recall misses and on any pending/rejected/superseded leak (FR-26/FR-30/FR-32) |
| Q→C→A quality (issue #74) | `scripts/evaluate_rag_quality.py` + `golden_queries.json` | Manual, host-side; not a CI gate | Real LibreChat Agent generation plus ordered `/debug/rag_search` contexts, scored by the local Ollama judge for contextual relevance/recall/precision, faithfulness, answer relevance/correctness, citation validity, and abstention behavior |
| Browser CSRF + logout (issue #187) | `scripts/verify_browser_csrf_logout.py` | `e2e.yml` job `browser-verify` (same `needs-e2e` gating as golden-query) | Real Chromium against a real `docker compose up`: HttpOnly/readable cookie attributes, missing/mismatched/matching `X-CSRF-Token` (NFR-14), and a full Keycloak RP-initiated logout actually ending the SSO session (issue #254) rather than just the server-side logic `services/ingestion-api/tests` already covers with `TestClient` |
| Tagging-advisory calibration | `scripts/calibrate_tagging_advisory.py` | Manual or scheduled (`docker compose --profile calibration run`), not in any CI workflow | Suggester-vs-curator agreement over time for Phase 1-3's advisories (FR-13/FR-16/FR-30/FR-32) — reporting only, no pass/fail gate by default |
| Reconnaissance-shaped query detection (issue #426) | `scripts/detect_query_anomalies.py` | Manual or scheduled (`docker compose --profile anomaly-detection run`), not in any CI workflow | Per-identity query-rate, denial-ratio, narrow-result-probing, and denial-then-success boundary-mapping signals over the audit log (#127 gap #4) — reporting only, no pass/fail gate; a content-free, bounded count per signal is also pushed to Pushgateway for `NexusRagQueryAnomalyDetected`/`NexusRagQueryAnomalyDetectionStale` |
| Mutation | `services/common/pyproject.toml` `[tool.mutmut]` | `e2e.yml` (nightly, **enforced ≥80% kill rate**, issue #78) | Test-suite strength on claims/access-filter/metadata/versioning |

Design notes:

- **No live infrastructure for unit/BDD layers.** JWTs are real RS256 tokens
  minted against an in-memory keypair (`tests/conftest.py`), verified through
  the production `parse_claims` code path with a stubbed JWKS client.
  Classification ranking runs against in-memory SQLite. This keeps the PR
  gate fast while still exercising the real security code.
- **Each service's `app` package collides by name** (every service ships a
  top-level `app`), which is why per-service unit tests live in
  `services/<name>/tests/` and run one service at a time (`service-tests` is
  a matrix job), and why `pytest.ini`'s `testpaths` deliberately stays at
  `tests`. Collecting two services in one process fails outright: the
  second's `from app... import` resolves against the first's package. The
  repo-root `tests/` tree therefore holds only what has no service directory
  of its own -- `services/common` and the cross-cutting BDD scenarios.
  (Issue #113: an earlier draft of this branch kept per-service unit tests
  under `tests/unit/<service>/` behind `sys.path` shims. They are merged into
  the in-service suites instead, which is where the same coverage was already
  accumulating on `main`, and where measuring it gives an honest number --
  gating the root copy alone reported 73% for the worker against the 95% the
  merged suite actually achieves.)
- **The golden-query job is byte-identical to local runs** — same
  `docker compose up`, same `--profile eval run --rm eval-retrieval` command
  documented in dev-setup.md.

## Running locally

```bash
python3 -m venv .venv-test && source .venv-test/bin/activate
pip install -r requirements-test.txt
pip install "pydantic>=2.7" "PyJWT>=2.8" "cryptography>=42.0" \
  "sqlmodel>=0.0.21" "qdrant-client>=1.10" "nats-py>=2.7" "boto3>=1.34"

pytest                                  # the repo-root tree (common + BDD)
# ^ If anything here behaves strangely, first verify the venv actually won:
#   `which python pytest pip mypy` must all resolve inside .venv-test. On a
#   distro whose system Python is older than 3.11 (e.g. RHEL 9's 3.9), a
#   system or ~/.local tool silently shadowing the venv produces misleading
#   failures far from the cause: pip "cannot find" pinned versions like
#   pytest==9.x or pyroscope-io==1.2.1 (their requires-python >= 3.11 hides
#   them from an old resolver entirely), and an old mypy reports bogus
#   datetime.UTC / zip(strict=) errors across services/common. The root
#   conftest.py refuses to collect under < 3.11 for exactly this reason.
#   Keep the venv's mypy at ci.yml's pinned version (mypy==2.3.0).
pytest tests/unit/common tests/e2e \
  --cov=common --cov-branch --cov-fail-under=85   # the enforced gate

# Per-service suites -- one service at a time, from its own directory (see
# the `app`-collision note above). `pytest` at the repo root does NOT reach
# these; ci.yml's service-tests job runs them as a matrix.
for svc in ingestion-api ingestion-worker orchestration-mcp; do
  (cd "services/$svc" && pytest tests -q)
done

ruff check services scripts tests       # lint gate
mypy services/common/common             # type gate (enforced scope)
(cd services/reranker-service && mypy app)  # type gate (enforced, issue #79)
(cd services/ingestion-worker && MYPYPATH=../common mypy app)  # ditto
(cd services/orchestration-mcp && MYPYPATH=../common mypy app)  # ditto
(cd services/ingestion-api && MYPYPATH=../common mypy app)  # ditto
python scripts/check_pinned_images.py   # NFR-16 floating-tag gate
bandit -r services scripts              # security static analysis
helm lint helm/nexus-rag                # chart gate

# Full-stack e2e (identical to e2e.yml's golden-query job):
docker compose up -d --wait
docker compose --profile eval run --rm eval-retrieval
```

The Q→C→A evaluator is deliberately host-side because LibreChat's dev OAuth
redirect is `https://localhost:3080`. After completing the CA/hosts setup and
logging the evaluation user into LibreChat once (see
[querying-the-corpus.md](querying-the-corpus.md)), create the per-user agent and
pass its printed ID to the evaluator:

```bash
scripts/create_librechat_agent.sh dave-admin
python scripts/evaluate_rag_quality.py \
  --agent-id <agent-id> --history-dir .eval-history/qca
```

The script refreshes the agent's MCP connection and the direct-retrieval bearer
before every case unless `--skip-connect` is given; this keeps a slow CPU run
from outliving Keycloak's 900-second dev token. It calls the real LibreChat →
LiteLLM/Ollama → `rag_search` path for each golden query and exits non-zero if
generation omitted the tool or any case could not be scored. Its local 3B/7B
judge is useful for comparing two
configurations, not for an absolute quality claim. Baselines are therefore
rejected when the judge model, judge-prompt version, or golden-set hash differs.

"Manual, host-side" in the table above is a property of the script, not just a
convention: its login and generation path come from
`adversarial_injection_probe.py`, whose endpoints and redirect URIs are
`localhost` literals. `docs/observability.md`'s "Running it unattended" (#388)
enumerates what would have to change for this to run on a schedule in a cluster,
and what credentials such a run would need.

**`abstention_accuracy` is diagnostic only — it is not a quality score (#386).**
Measured on the travel-policy-abstention case with the four approved
sample-corpus documents as context, 3 runs per cell:

| judge | judge prompt | answer correctly abstains | answer **fails** to abstain |
|---|---|---|---|
| `qwen2.5:3b-instruct` | `qca-v1` | `null` 3/3 — run reported FAILED | `null` 3/3 |
| `qwen2.5:3b-instruct` | `qca-v2` | `true` 3/3 | `false` 1/3, `true` 2/3 |
| `qwen2.5:7b-instruct` | `qca-v2` | — | `true` 3/3 |

`qca-v2` fixes the case the shipped golden set actually exercises — both
`expected_abstention` cases have no on-topic evidence, so a working pipeline
abstains and the judge now says so.

What no judge tested here fixes is the other direction: **catching an answer
that should have abstained and didn't.** Shown an answer that invented a
reimbursement policy outright, the 3B judge called it a correct abstention 2
times in 3, and the 7B judge 3 times in 3 — no better, and in that test worse.
An earlier version of this document suggested `--judge-model
qwen2.5:7b-instruct` as a mitigation for exactly this; that claim was never
measured and the measurement does not support it, so it is gone rather than
softened.

The consequence is deliberate and encoded, not just documented:
`abstention_accuracy` is **excluded from `_COMPARED_METRICS`**, so it does not
participate in baseline comparison and cannot raise or suppress a regression
verdict. It is still computed, reported, and published — a `0.0` there is worth
investigating — but a `1.0` is not evidence the pipeline declines when it
should. Read it as a smoke signal, not a gate.

A judge that will not commit to true/false is recorded as **undetermined** —
excluded from `abstention_accuracy`, counted in `abstention_undetermined`, and
printed as a warning — rather than failing the run, because a judge declining to
assert confidence is not the same failure as generation regressing or the tool
not being called. Note that `abstention_accuracy` alone cannot tell you coverage
was partial: a run with one determined pass and one undetermined case still
reads `1.0`, which is why the count is reported next to it.
Reports contain hashes, aggregate counts, and scores by default—not query,
source names, context, reference-answer, or generated-answer text.
`--include-content` is an explicit diagnostic option whose output must be
handled at the corpus's classification.

## Workflows

- **`.github/workflows/ci.yml`** (PR + push to main): unit + BDD on Python
  3.11/3.12 with an enforced **≥85% line+branch coverage** floor (scope
  below), `ruff check`, `mypy` (enforced across `services/common` and all
  four app services as of issue #79), the NFR-16 image-pin check, and a full
  `docker compose build` of all custom images.
- **`.github/workflows/e2e.yml`** (nightly, manual, and a PR labeled
  `needs-e2e`): full-stack golden-query e2e; browser-verify (issue #187,
  same gating as golden-query -- see above); the containerized integration
  layer (issue #428, same gating -- see below); mutation testing (enforced
  ≥80% kill rate as of issue #78, nightly/manual only regardless of label,
  see below). Reports uploaded as artifacts.
- **`.github/workflows/security.yml`** (PR + weekly): `bandit`,
  `pip-audit` against the shipped dependency tree (the test toolchain is
  dev-only and excluded), `helm lint` + `helm template`, Trivy filesystem
  scan (HIGH/CRITICAL, unfixed ignored); weekly: Trivy image scans of the
  four built images.
- **`.github/workflows/codeql.yml`** (PR + push to main + weekly): checked-in
  ("advanced setup") CodeQL analysis with the `security-extended` query
  suite, replacing GitHub's implicit default setup. It deliberately triggers
  on `pull_request` rather than `pull_request_target` — the default setup
  does not run at all on PRs opened from a fork, which left every
  fork-authored PR permanently unable to satisfy a required `CodeQL` check
  (hit in practice on #64-#67). Triggering here gives fork PRs the same
  scoped, read-only-plus-`security-events:write` token GitHub grants for this
  case, without widening what fork-authored code can otherwise touch.
- **`.github/dependabot.yml`**: weekly pip (per service + test toolchain),
  GitHub Actions, and Docker base-image updates. Issue #230: each service's
  pip directory also carries a hash-pinned `requirements.txt` (compiled
  from `requirements.in` via `pip-compile --generate-hashes`), installed
  with `pip install --require-hashes` in that service's Dockerfile instead
  of the floor ranges in `pyproject.toml` directly — Dependabot updates
  both the floors and the lockfile hashes from the same directory entry.
  reranker-service's lockfile excludes `torch` (installed separately from
  a configurable CPU/CUDA index; see that service's `requirements.in` and
  Dockerfile for why hash-pinning it would defeat that toggle) — it was
  compiled with a local stub `torch` package (version `999.0.0`, no
  dependencies) supplied via `--find-links` so the resolver picks it over
  the real package without needing torch's own transitive deps, then that
  stub's own entry was deleted from the resulting `requirements.txt` by
  hand; regenerating that one lockfile needs the same trick, not a plain
  `pip-compile` run. Every other lockfile regenerates after a
  `pyproject.toml` floor edit with
  `pip-compile --generate-hashes --output-file=requirements.txt requirements.in`
  run inside a `python:3.11-slim` container (matching the Dockerfile's base
  image) from that service's directory.

## Branch protection (merge gates)

Two repository rulesets, both targeting `refs/heads/main` and both `active`,
jointly enforce PR-only, non-fast-forward merges and a set of required status
checks — the mechanism that makes the gates above actually *block* a merge
rather than being advisory-only (issue #81; #60's premise that verification
is enforced, not just available). Applied 2026-07-27.

- `Protect-Main` — the original ruleset: PR required, no fast-forward/deletion,
  the required-checks list below, non-strict.
- `Protect-Main-Strict-Status-Checks` — added alongside it (not merged into
  it) to carry `strict_required_status_checks_policy: true` plus the same
  checks list, for the reason in "Applying ruleset changes" below. GitHub
  enforces multiple matching rulesets additively, so the net effect is one
  set of required checks, now with strict enforcement, from two rulesets.
  Verified against a currently-open PR: one behind `main` shows
  `mergeStateStatus: BEHIND` and is blocked from merging even though its
  checks already passed.

**Required checks** (every job below runs unconditionally on every PR, no path
filter, so a required check can never be left permanently "waiting"):

- `CodeQL` (GitHub code-scanning integration check, distinct from the
  `Analyze (python)` workflow job it's derived from)
- `unit (3.11)`, `unit (3.12)`
- `service-tests (ingestion-api)`, `service-tests (ingestion-worker)`,
  `service-tests (orchestration-mcp)`, `service-tests (reranker-service)`
  (issue #256: the job now carries an explicit `name: service-tests
  (${{ matrix.service }})`, so these four context strings are stable —
  editing the per-service `cov` flags in `ci.yml`'s matrix no longer renames
  the check. `reranker-service` joins the required list here for the first
  time; it already ran unconditionally with real tests, just wasn't pinned.)
- `lint`, `types`, `pin-check`, `build` (all `ci.yml`)
- `bandit`, `pip-audit`, `helm`, `trivy-fs`, `secret-scan` (all `security.yml`)

Branches must be up to date with `main` before merging (strict status
checks) — enabled via `Protect-Main-Strict-Status-Checks` above, per the
issue's suggested direction. This adds rebase friction to every Dependabot
PR that isn't first in the merge queue; if that friction outweighs the
value in practice, disable it there rather than reintroducing a second
source of truth for the checks list.

**Deliberately not required: `golden-query` and `mutation`.** Both live in
`e2e.yml`. `golden-query` used to be path-filtered (`services/**`,
`scripts/**`, `infra/**`, `docker-compose.yml`, the workflow file itself),
triggering on nearly every real PR; issue #301 replaced that with a
job-level `if` gated on the PR carrying a `needs-e2e` label (or the nightly/
manual triggers, which always run it) — the full compose boot plus the
`ollama-model-init` pull was too slow to pay by default on PRs that mostly
can't regress retrieval. A required status check that never fires for a
given PR (e.g. a docs-only change, or a code PR nobody labeled) would leave
GitHub's merge button permanently stuck on "Expected — waiting for status to
be reported" — the same class of bug the fork-PR CodeQL fix above addresses,
just triggered by a conditional trigger instead of a fork-token limitation.
A *skipped* job (the `if` false case) does report a status, so this
specific mechanism wouldn't reproduce that bug on its own — but making
`golden-query` required would still mean either labeling every single PR
`needs-e2e` (defeating the point of the opt-in) or accepting that most PRs
merge without this coverage, which is exactly today's tradeoff stated
plainly rather than made required and then quietly never enforced.
`mutation` stays nightly/manual-only regardless of the label, but is no
longer advisory — since #78 it fails its own run below an 80% kill rate
(see "Mutation testing" below).

**Fork-PR CodeQL reporting, confirmed working.** Issue #81's comment flagged
an open question: does the default CodeQL setup produce a check run on
fork-originated PRs at all? It didn't at the time (#64-#67 hit exactly this).
The checked-in `codeql.yml` above already fixes it: five current
fork-authored PRs (#161, #167, #168, #169, #170) all show both `CodeQL` and
`Analyze (python)` passing, so no further action was needed on that half of
the issue.

**Issue #256 (check names decoupled from coverage flags).** The
`service-tests` job previously had no explicit `name:`, so GitHub derived the
check name from the matrix values, embedding the `cov` flags in the pinned
context string (e.g. `service-tests (ingestion-worker, --cov=app.chunking
--cov=app.parsing)`). Editing those flags renamed the check out from under
the rulesets, and the pinned context then never reported — every PR stuck on
"Expected — waiting for status to be reported" with nothing red to fix. Fixed
by pinning `name: service-tests (${{ matrix.service }})` on the job, so the
four stable names above can be pinned once and the `cov` matrix can change
freely going forward. `app.ocr`, previously routed around this trap via
ingestion-worker's own `addopts` (#241), moved back into `ci.yml`'s matrix now
that doing so is safe, so the gated-module list lives in one place again.
Landing this needed a deliberate merge-blocking window, since the PR making
the change can't satisfy the required-check names it's renaming: an admin
removed the old `service-tests (…)` contexts from both rulesets, the workflow
PR merged, then the admin re-pinned the four stable names above (adding
`reranker-service` to the required list for the first time in the process).

**Applying ruleset changes.** The ruleset REST API's `PATCH` endpoint 404s
for at least one token type (an OAuth-app token with `repo` scope) even with
admin permission on the repo — `GET`, `POST` (create), and `DELETE` all work
fine against that same token. It works as a classic/fine-grained PAT or via
the Settings → Rules UI, which is how `Protect-Main`'s required-checks list
was applied. `Protect-Main-Strict-Status-Checks` was instead added as a
second ruleset via `POST`, specifically to enable strict mode *without*
`PATCH` or a delete-then-recreate of the already-active `Protect-Main` —
deleting a live branch-protection ruleset, even to immediately recreate it,
is a destructive action on shared infrastructure that's better avoided than
risked on a token-quirk workaround. If a `PATCH`-capable credential becomes
available later, folding both rulesets back into one is a cleanup, not a
requirement.

## Containerized integration layer (issue #428)

`tests/unit`/`tests/e2e` mock or use in-memory SQLite (no live infra); the
golden-query/browser-verify jobs boot the *entire* app stack (all four
custom services, LibreChat, Ollama). Neither is the right shape for a
property that needs one specific piece of real infra and nothing else --
`tests/integration/` is that middle tier, run by `e2e.yml`'s `integration`
job under the same opt-in `needs-e2e` gating as golden-query/browser-verify
(a role/grant bootstrap plus an `ingestion-api` image build is too slow to
pay on every PR touching the stack).

**What it covers today: NFR-2 only.** `tests/integration/test_nfr2_audit_log_
append_only.py` connects directly (no app code in the loop) as each of the
three application database roles and the dedicated audit-reporting role, and
asserts against a live Postgres that `INSERT`/`SELECT`/`UPDATE`/`DELETE`
succeed or fail exactly as `infra/postgres/grant-matrix.sql` says they
should. This is the property a mock structurally cannot prove --
`tests/unit/common` runs against in-memory SQLite, which has no privilege
system to enforce, so a regression in the grant scripts (accidentally
granting `UPDATE` to an application role, say) would pass every existing
test and previously would only have been caught live/manually. Verified to
actually catch that class of regression: manually re-granting `SELECT` to
`nexus_rag_ingestion_api` on a live stack and rerunning the suite turns
`test_select_denied[ingestion-api]` red with "DID NOT RAISE"; revoking it
again turns the suite back green.

**What it doesn't cover yet, and why:** the job stands up Postgres only,
not Qdrant/NATS/Keycloak, because nothing in `tests/integration/` today
exercises them -- standing up unused containers "for completeness" would
just be padding. Two follow-ups extend this:

- **Issue #439** (NFR-11 crash-redelivery, NFR-13 live revert-on-partial-
  failure): needs Qdrant + NATS, and a fault-injection seam that doesn't
  exist yet. `docs/testing.md` and issue #77 previously noted this
  deliberately wasn't added, to keep production code free of test-only
  branches -- #439 tracks the design decision on how to add one (or avoid
  needing one) before writing the tests.
- **Issue #440** (`ingestion-api` route-layer tests, `orchestration-mcp/app/
  rag_search.py`): needs Qdrant + Keycloak, to replace the mocked
  equivalents currently standing in for a real vector store and real OIDC
  tokens in those two modules' "measured but not gated" coverage (see
  "Coverage policy" below).

**Reproducing the job locally**, e.g. to debug a failure or extend the
suite: `docker-compose.ci-integration.yml` overlays a host-reachable port
onto Postgres (the base `docker-compose.yml` deliberately gives it none --
every other service reaches it over the compose network as `postgres:5432`,
and nothing outside this job's own pytest process needs it from the host).

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.ci-integration.yml
docker compose up -d postgres
# Same bootstrap sequence a normal `up` runs via depends_on, driven directly
# since this job never starts ingestion-api (whose healthcheck the compose
# graph's lock-down-db-grants normally waits on) -- --no-deps skips that
# dependency once migrate-db-schema has created the tables it actually needs:
docker compose run --rm ensure-db-roles
docker compose run --rm migrate-db-schema
docker compose run --rm --no-deps grant-service-privileges
docker compose run --rm --no-deps lock-down-db-grants

POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 pytest tests/integration -v

docker compose down -v
```

Without a live Postgres reachable (e.g. running `pytest tests/integration`
directly against a workstation with nothing listening on `localhost:5432`),
the whole tree skips cleanly with a message pointing back at this section,
rather than failing with a confusing connection error --
`tests/integration/conftest.py`'s `_require_live_postgres` fixture.

## Coverage policy

The enforced `--cov-fail-under=85` applies to:

- `services/common` — **excluding** `db.py`, `qdrant_store.py`, and
  `sparse_embedding.py` (see `.coveragerc`), which require live
  Postgres/Qdrant or a model download and are therefore covered by the
  compose-level e2e, not the unit gate. Everything else in `common` measures
  ~99% today.
- `app.chunking` + `app.parsing` + `app.ocr` (ingestion-worker) — the pure
  FR-3/FR-4 logic. `app.processing`/`app.embedding` are pipeline glue
  exercised by the e2e job. `app.ocr` (#241) lives in ci.yml's matrix `cov`
  string alongside `app.chunking`/`app.parsing`; `app.captioning` (#92) stays
  in the service's own `[tool.pytest.ini_options] addopts` since it's outside
  this coverage floor. Both used to route around the matrix string entirely,
  since it doubled as the required check's name and editing it renamed the
  check out from under the rulesets — fixed by #256, which pins an explicit
  job `name:` instead (see "Branch protection" above).
- `app.reranking` (orchestration-mcp) — 100% today.

The ingestion-api route layer and `rag_search.py` are intentionally measured
but not yet gated — they need the integration layer (live Postgres/Qdrant/
Keycloak via containers) to test meaningfully. Issue #428 added that layer
(see "Containerized integration layer" above); pointing these two at it and
folding them into this gate is issue #440. That is a real gap, stated
plainly rather than hidden behind a lower repo-wide number.

## Mutation testing (enforced ≥80%)

`e2e.yml`'s `mutation` job runs `mutmut` against `claims.py`,
`qdrant_filters.py`, `metadata.py`, and `versioning.py` — the modules where a
subtle logic change is a security incident — and **fails below an 80% kill
rate** (`scripts/check_mutation_score.py`, issue #78). Baseline at
enforcement time: **88.0%** (183 mutants, 161 killed), with every survivor
triaged — the real gaps became tests (`groups` was never asserted out of
`parse_claims`, the username→sub fallback was unpinned, the JWKS test stub
ignored its argument) and the rest were error-message mutants killed by
switching substring asserts to exact-message asserts. The gate parses the
final tally line of the teed `mutmut run` output (mutmut 3.x has no
machine-readable summary) and **fails closed** when there is no tally —
which is not hypothetical: the advisory (`continue-on-error`) era of this
job never once produced a score. Every night ended in `failed to collect
stats` (and later an outright crash: `test_file_types.py`'s cross-service
drift guard reads `ingestion-worker/app/parsing.py` by path, which doesn't
exist inside mutmut's staged copy — it now skips itself under that staging),
and nothing was red anywhere. "Timeout", "suspicious", and "no tests" all
count against the score; skipped mutants are excluded from the denominator.

Config lives in `services/common/pyproject.toml` (`[tool.mutmut]`,
`source_paths` scoped to the four modules) and the job runs from that
directory (`cd services/common && mutmut run`) — running from the
repo root makes mutmut derive module names as `services.common.common.*`
while the tests import `common.*`, so nothing matches and every mutant
spuriously survives. Two related gotchas, both encoded in the repo:
`services/common` must NOT be pip-installed in the mutation job (the
installed package would shadow the mutated copy), and `tests/conftest.py`'s
sys.path shim disables itself when `MUTANT_UNDER_TEST` is set.

The job stays nightly/manual-only (it is not part of PR CI, see "Branch
protection"), so "enforced" means a red nightly `e2e` run that someone must
triage — either a test weakened or a survivor slipped in — not a merge
blocker on individual PRs.

## Retrieval-quality tracking & re-evaluation policy (FR-30/FR-32)

`scripts/evaluate_retrieval.py` runs the golden-query set through the real
`rag_search` pipeline and reports recall@K / precision@K plus the forbidden-leak
check. On its own that is a point-in-time snapshot; FR-30 wants quality tracked
*over time* so degradation is visible rather than silent, and FR-32 wants
re-evaluation tied to the changes that can cause it.

**Trend store + regression gate.** The harness supports this directly:

```bash
# Persist each run under a timestamped filename (the trend store) and fail if
# recall@K/precision@K drop below the most recent prior run.
python scripts/evaluate_retrieval.py --history-dir eval-history

# Or diff against an explicit committed baseline, allowing a small noise band.
python scripts/evaluate_retrieval.py \
  --baseline eval-history/retrieval-eval-<stamp>.json --regression-tolerance 0.02
```

With `--history-dir`, each run is kept (not overwritten) and the previous run is
used as the baseline; a drop beyond `--regression-tolerance` exits non-zero, the
same way a forbidden leak already does. The pure history/baseline logic is unit
tested in `tests/unit/test_evaluate_retrieval.py`; the scoring itself is covered
by the golden-query e2e job.

**Re-evaluation triggers (FR-32).** Re-run the harness, and refresh the
baseline, on any of:

- **A change to the embedding or reranker model pin (NFR-16)** — a different
  embedding model changes the vector space and a different cross-encoder changes
  ranking, either of which can move recall silently. This is mandatory: treat a
  model-pin PR as incomplete until the golden-query run is green against the
  prior baseline.
- **A change to chunking or retrieval logic** (`ingestion-worker` chunking,
  `orchestration-mcp` fusion/rerank).
- **The nightly `e2e.yml` schedule**, which covers the "periodic" cadence.

## Tagging-advisory calibration (FR-13/FR-16/FR-30/FR-32, issue #309)

Phase 1-3 of #138 (`marking_detection.py`, `find_similar_approved` precedent
lookup, `classification_suggestion.py`'s LLM zero-shot suggester) each flag a
possible under-classification to the curator, and `curate.py` embeds the
flag plus the curator's final decision in that document's `document.approve`/
`document.reject` audit entry. `scripts/calibrate_tagging_advisory.py` mines
that trail and reports, per suggester, how often the curator's final decision
agreed with the flag vs. overrode it:

Issue #345 extends this to the sensitive-data-pattern advisory family (#342's
regex pass, #343's LLM-assisted pass): `pii_regex`/`pii_llm` in the report.
Unlike the classification-tag suggesters above, a PII finding carries no
classification/releasability target to rank-compare against, so "agreement"
there means something different — see
`scripts/calibrate_tagging_advisory.py`'s module docstring and `PiiTally` for
the `acted_on_rate` definition (rejected or approved-with-a-changed-tag counts
as acted on; approved-unchanged does not).

Issue #380 adds `pii_regex_llm_verdict` (`PiiVerdictTally`) on top of that:
#378 added an `llm_verdict` (`likely_false_positive` + rationale) to each
Phase 1 regex finding, judged from context alone. Unlike `pii_regex`/`pii_llm`
above, the verdict *is* an actual prediction to score, so this one uses
`agreement_rate` like the classification-tag suggesters — did the curator's
decision agree with what the verdict predicted, for the (common) case where
every finding in a document landed on the same verdict? A document with mixed
verdicts across its findings, or only-partial verification coverage, is
counted in `skipped` rather than scored.

```bash
# Persist each run under a timestamped filename and print a trend line
# against the previous one.
python scripts/calibrate_tagging_advisory.py --history-dir calibration-history

# Or via the compose one-shot, against the dev stack's own Postgres.
docker compose --profile calibration run --rm calibrate-tagging-advisory
```

Deliberately **not** a pass/fail CI gate the way `evaluate_retrieval.py` is:
a curator overriding a flag is not, by itself, evidence the suggester was
wrong (that's the point of keeping a human in the loop, FR-11). It's
reporting only unless a deployment opts into `--min-agreement` as its own
floor. The pure aggregation logic is unit tested in
`tests/unit/test_calibrate_tagging_advisory.py` against constructed audit
rows; the DB fetch itself needs a live Postgres seeded with real curator
decisions and has not been exercised end to end (see
`docs/dev-setup.md`'s "What's stubbed vs working").

## Reconnaissance-shaped query detection (issue #426, #127 gap #4)

FR-31 records every `query`/`query.denied` audit row, and #72/#73 shipped
metrics and SIEM export, but nothing read either for the threat #127 names
explicitly: an authorized `rag-query` user probing with crafted queries to
infer whether a specific document exists in the corpus, including one
outside their own filter. `scripts/detect_query_anomalies.py` mines the same
`nexus_rag_audit_reporting` trail `calibrate_tagging_advisory.py` uses (no
new grant) and flags, per identity over a lookback window: a raw attempt-rate
spike (`high_volume`), a sustained personal denial rate distinct from the
global `NexusRagQueryDeniedSpike` volume alert (`high_denial_ratio`), a high
share of successful queries resolving to 0-1 chunks (`narrow_probe_shaped` —
the substitute for near-duplicate-query-text detection, since #125 means
there is no query text to diff), and repeated denial-then-success sequences
within a short window (`boundary_mapping` — narrower than "filter-boundary
mapping" sounds; confirmed live that `rag_search.py`'s only `query.denied`
path is the coarse missing-`rag-query`-role gate, not a per-query FR-26
mismatch, so this actually flags a `rag-query` grant changing state
mid-window and being used immediately after).

```bash
python scripts/detect_query_anomalies.py --lookback-minutes 60

# Or via the compose one-shot, against the dev stack's own Postgres.
docker compose --profile anomaly-detection run --rm detect-query-anomalies
```

Reporting only, same posture as the calibration script above. What reaches
Prometheus (via Pushgateway, same mechanism `publish_rag_quality_metrics.py`
uses) is a *count* of flagged identities per signal plus a staleness
timestamp — deliberately no `actor_sub`/`actor_username` label, for the same
cardinality/privacy reason `orchestration-mcp/app/metrics.py` gives for never
labeling a metric by user. Attribution — which identity was actually flagged
— is only in the script's own stdout report, read by whoever holds the
`nexus_rag_audit_reporting` credential (`docs/governance.md`'s "Query
confidentiality and user privacy" names that audience). The pure aggregation
and exposition logic is unit tested in
`tests/unit/test_detect_query_anomalies.py` against constructed audit rows,
and the full path — DB fetch, Pushgateway export, and the resulting
Prometheus alert — has been **validated against a live environment** (see
`docs/dev-setup.md`'s "What's stubbed vs working").

Connects to Postgres as its own dedicated, SELECT-only-on-`audit_log` role
(`nexus_rag_audit_reporting`) rather than through any of the four services —
see that script's module docstring for why (NFR-2 keeps every application
role's own credentials INSERT-only on `audit_log`).

## Access-control corpus (classification matrix)

The golden-query harness asks whether the right document is *retrievable*.
`scripts/create_classification_corpus.py` builds a corpus for the adjacent
question: whether each persona sees exactly the documents their claims entitle
them to, at a realistic corpus size and across the whole classification ladder.

Fifteen documents — five formats (`txt`, `md`, `html`, `pdf`, `docx`) at each of
`UNCLASSIFIED`, `CUI` and `SECRET`, with the releasability rotated so no two
documents at a level share a holding. Each is ~4KB of prose, which chunks two to
five times; a single-chunk document cannot show a ranking or partial-visibility
bug.

Attribution is by **canary phrase, not filename**. Every document repeats a
unique token (`MARBLE-HORIZON-7` and friends) through its body, so a hit is
attributable to exactly one source even when two documents share a filename at
different classifications — the failure mode issue #226 originally recorded in
the golden-query harness.

`scripts/evaluate_retrieval.py` no longer has that failure mode either: its
leak check now asserts each returned chunk's own `status` payload field is
`"approved"` (the access filter's own invariant, `common/qdrant_filters.py`)
rather than matching `golden_queries.json`'s `forbid` filenames against
returned filenames. A duplicate filename across two documents in different
states — filenames are explicitly non-unique in the data model, so two
independent submissions can collide — can no longer produce a false FR-26
alarm, since the check no longer depends on filename identity at all. The `forbid`
list is still checked, but only as an informational `content_overlap` note in
the report; it is not what fails the build.

```bash
python scripts/create_classification_corpus.py      # regenerate (needs python-docx)
docker compose --profile corpus run --rm ingest-classification-corpus
docker compose --profile corpus run --rm verify-corpus-access
```

Both steps run **inside the compose network** deliberately: a token minted
against `127.0.0.1:8080` carries that issuer, the services verify against
`http://keycloak:8080`, and the mismatch surfaces as a bare 401 that is
confusing to diagnose from the host.

`verify_corpus_access.py` derives each persona's expected visibility from the
manifest and the realm's role grants rather than hard-coding it, so adding a
document to the matrix extends the test without editing it. A `LEAK` is an FR-26
access-control defect; a `MISSING` is an ingestion gap or a recall problem.

Validated against the live stack: `bob-query` and `carol-curator` (SECRET,
FVEY/NATO) each saw 9 of 9 permitted documents and none of the six NOFORN/USA
ones; `dave-admin` (SECRET, all four holdings) saw 15 of 15.

That run predates issue #229: at the time, all three classification levels shared
one Qdrant collection -- 72 points in `nexus_rag_chunks`, 24 UNCLASSIFIED / 27 CUI
/ 21 SECRET -- which was the evidence behind #229's proposal to split it.

**#229 status: implemented, unit-tested against a fake Qdrant client, not yet
validated against the live stack.** `common/qdrant_store.py` now derives one
collection per Classification value (`classification_collection_name`), routes
ingestion/curation/supersession/purge through it, and `qdrant_backend.py` fans
`hybrid_query` out over every collection the caller is cleared for, fusing
results by rank (`common/vector_store.fuse_ranked`) rather than by score --
see `tests/unit/common/test_classification_collections.py`,
`test_qdrant_backend_fanout.py`, and `test_rrf_fusion.py` for the pure-logic
coverage (collection naming/routing, the classification-correction migration
path including its partial-failure case, and the fusion arithmetic). What that
coverage cannot exercise -- and what a `docker compose up` + golden-query run
still needs to confirm before this is called *validated against a live
environment* rather than merely *implemented* (see this file's confidence-label
convention) -- is real Qdrant behavior: collections actually created on demand,
`scroll`/`upsert`/`delete` pagination against a live server, and whether recall
holds once BM25's IDF is computed per classification-skewed collection instead
of over the whole corpus. **`scripts/golden_queries.json` has not been
re-baselined against the split** -- the issue calls this out explicitly as part
of the work, not something to discover when CI goes red, and it remains open:
whoever next runs the live e2e job against this change should expect the
`--baseline`/`--regression-tolerance` comparison (see above) to need a fresh
baseline capture, not a regression fix.

## Known gaps / follow-ups

- Wiring the trend store into CI so nightly runs retain history *across* runs
  (download the prior artifact or commit a baseline) and gate on it — the
  harness supports it (issue #71); the cross-run persistence in `e2e.yml` is the
  remaining step.
- Issue #428 added the containerized integration layer itself
  (`tests/integration/`, `e2e.yml`'s `integration` job) and closed its
  NFR-2 append-only-audit-enforcement slice with a live-Postgres regression
  test — see "Containerized integration layer" above. Two slices remain,
  now tracked as their own issues rather than folded into this one:
  **NFR-11 crash-redelivery** and **NFR-13's live revert-on-partial-failure**
  (issue #439) — NFR-13 already has a committed mock-based regression test
  (`services/ingestion-api/tests/test_curate_nfr13_revert.py`, issue #77);
  the remaining gap there is specifically a live run against a real
  Postgres/Qdrant pair, which needs a fault-injection hook (deliberately not
  added yet, to keep production code free of test-only branches) — and
  **`ingestion-api` route tests / `rag_search.py`** against real containers
  instead of mocks, with their coverage folded into `ci.yml`'s gate
  (issue #440).
- The LibreChat OIDC browser E2E remains blocked on the Keycloak admin step
  noted in dev-setup.md.
- Issue #230's hash-pinned lockfiles cover the four services' and scripts'
  *Dockerfiles* only. `ci.yml`'s own `pip install "pkg>=x"` lines (unit,
  service-tests, types jobs) and `security.yml`'s `pip-audit` job install
  the same floor ranges independently and unpinned by hash — deliberately
  out of scope here (those jobs need the latest compatible version to
  catch a regression/CVE early, which a hash-pinned lockfile would work
  against), but worth knowing the lockfiles aren't a single source of
  truth for every dependency install path in this repo.
