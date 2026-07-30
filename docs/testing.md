# Testing & CI Quality Gates

How this repo verifies itself: the local/CI test pyramid, what each GitHub
Actions workflow enforces, and the honest current state of each gate. Related:
[docs/dev-setup.md](dev-setup.md) for the live-stack walkthrough, and
[scripts/evaluate_retrieval.py](../scripts/evaluate_retrieval.py) for the
golden-query harness the e2e workflow reuses.

## The pyramid

| Layer | Where | Runs in | What it pins |
|---|---|---|---|
| Unit | `tests/unit/common/` | `ci.yml` (every PR, py 3.11 + 3.12) | Claims parsing & signature verification, the FR-26 access filter, FR-18 metadata enforcement, classification ranking, FR-7 supersede guards, object store (incl. path-traversal), job-queue publishing |
| Unit | `services/*/tests/` | `ci.yml` job `service-tests` (one invocation per service) | FR-4 chunking (boundaries/overlap, atomic tables, oversized chunks), FR-3/NFR-7 parsing (incl. zip-bomb guard, table extraction), FR-25 reranking (incl. degraded-mode fallback), plus the #106-#109 regression guards |
| BDD security scenarios | `tests/e2e/features/access_control.feature` | `ci.yml` (in-process, no stack needed) | The Section 6 invariants as readable Gherkin: approved-only status, clearance ceiling, releasability holdings, cross-org isolation, curator scoping, supersede guards |
| Retrieval quality + leak check | `scripts/evaluate_retrieval.py` + `golden_queries.json` | `e2e.yml` (nightly + PRs touching the stack) | Full `docker compose up` → seed → golden-query run; fails on recall misses and on any pending/rejected/superseded leak (FR-26/FR-30/FR-32) |
| Mutation | `services/common/pyproject.toml` `[tool.mutmut]` | `e2e.yml` (nightly, **advisory**) | Test-suite strength on claims/access-filter/metadata/versioning |

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

## Workflows

- **`.github/workflows/ci.yml`** (PR + push to main): unit + BDD on Python
  3.11/3.12 with an enforced **≥85% line+branch coverage** floor (scope
  below), `ruff check`, `mypy` (enforced across `services/common` and all
  four app services as of issue #79), the NFR-16 image-pin check, and a full
  `docker compose build` of all custom images.
- **`.github/workflows/e2e.yml`** (nightly, manual, and PRs touching
  `services/`, `scripts/`, `infra/`, `docker-compose.yml`): full-stack
  golden-query e2e; mutation testing (advisory, see below). Reports uploaded
  as artifacts.
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
  GitHub Actions, and Docker base-image updates.

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
- `service-tests (ingestion-api)`,
  `service-tests (ingestion-worker, --cov=app.chunking --cov=app.parsing)`,
  `service-tests (orchestration-mcp, --cov=app.reranking)`
- `lint`, `types`, `pin-check`, `build` (all `ci.yml`)
- `bandit`, `pip-audit`, `helm`, `trivy-fs`, `secret-scan` (all `security.yml`)

Branches must be up to date with `main` before merging (strict status
checks) — enabled via `Protect-Main-Strict-Status-Checks` above, per the
issue's suggested direction. This adds rebase friction to every Dependabot
PR that isn't first in the merge queue; if that friction outweighs the
value in practice, disable it there rather than reintroducing a second
source of truth for the checks list.

**Deliberately not required: `golden-query` and `mutation`.** Both live in
`e2e.yml`, which is path-filtered (`services/**`, `scripts/**`, `infra/**`,
`docker-compose.yml`, the workflow file itself) and also runs on a nightly
schedule. A required status check that never fires for a given PR (e.g. a
docs-only or workflow-only change that doesn't touch those paths) leaves
GitHub's merge button permanently stuck on "Expected — waiting for status to
be reported" — the same class of bug the fork-PR CodeQL fix above addresses,
just triggered by a path filter instead of a fork-token limitation. `mutation`
is additionally still advisory pending a baseline (see below). Making
`golden-query` a merge gate would mean either dropping its path filter (paying
its ~5-6 minute full-stack-compose cost on every PR, including doc-only ones)
or accepting that gap — worth revisiting once there's an owner for that
tradeoff, but out of scope here.

**Fork-PR CodeQL reporting, confirmed working.** Issue #81's comment flagged
an open question: does the default CodeQL setup produce a check run on
fork-originated PRs at all? It didn't at the time (#64-#67 hit exactly this).
The checked-in `codeql.yml` above already fixes it: five current
fork-authored PRs (#161, #167, #168, #169, #170) all show both `CodeQL` and
`Analyze (python)` passing, so no further action was needed on that half of
the issue.

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

## Coverage policy

The enforced `--cov-fail-under=85` applies to:

- `services/common` — **excluding** `db.py`, `qdrant_store.py`, and
  `sparse_embedding.py` (see `.coveragerc`), which require live
  Postgres/Qdrant or a model download and are therefore covered by the
  compose-level e2e, not the unit gate. Everything else in `common` measures
  ~99% today.
- `app.chunking` + `app.parsing` + `app.ocr` (ingestion-worker) — the pure
  FR-3/FR-4 logic. `app.processing`/`app.embedding` are pipeline glue
  exercised by the e2e job. `app.ocr` (#241) is added by the service's own
  `[tool.pytest.ini_options] addopts` rather than by ci.yml's matrix, because
  that matrix string *is* part of the required check's name (see the
  required-checks list above): editing it renames the check, the context both
  `Protect-Main*` rulesets pin then never reports, and every open PR blocks on
  "Expected — waiting for status to be reported" until an admin re-pins it.
  Making the names independent of the flags needs that same coordinated
  ruleset edit, so it is tracked as #256 rather than done here.
- `app.reranking` (orchestration-mcp) — 100% today.

The ingestion-api route layer and `rag_search.py` are intentionally measured
but not yet gated — they need the integration layer (live Postgres/Qdrant/
Keycloak via containers) to test meaningfully, which is tracked as a
follow-up. That is a real gap, stated plainly rather than hidden behind a
lower repo-wide number.

## Mutation testing (advisory for now)

`e2e.yml`'s `mutation` job runs `mutmut` against `claims.py`,
`qdrant_filters.py`, `metadata.py`, and `versioning.py` — the modules where a
subtle logic change is a security incident. It is `continue-on-error: true`
until a baseline score is established from a few nightly runs; the target is
**≥80% killed on `services/common`** before it becomes a merge gate. Config
lives in `services/common/pyproject.toml` (`[tool.mutmut]`) and the job runs
from that directory (`cd services/common && mutmut run`) — running from the
repo root makes mutmut derive module names as `services.common.common.*`
while the tests import `common.*`, so nothing matches and every mutant
spuriously survives. Two related gotchas, both encoded in the repo:
`services/common` must NOT be pip-installed in the mutation job (the
installed package would shadow the mutated copy), and `tests/conftest.py`'s
sys.path shim disables itself when `MUTANT_UNDER_TEST` is set.

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
states — e.g. from re-running the non-idempotent `seed_sample_data.py`
against an already-seeded stack — can no longer produce a false FR-26 alarm,
since the check no longer depends on filename identity at all. The `forbid`
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
- Integration layer with containerized Postgres/Qdrant/NATS/Keycloak
  (NFR-11 crash-redelivery, NFR-2 append-only audit enforcement are only
  covered live/manually today). NFR-13 revert-on-partial-failure now has a
  committed mock-based regression test
  (`services/ingestion-api/tests/test_curate_nfr13_revert.py`, issue #77) —
  the remaining gap there is specifically a live run against a real
  Postgres/Qdrant pair, which needs a fault-injection hook (deliberately not
  added yet, to keep production code free of test-only branches) rather than
  just this integration layer.
- The LibreChat OIDC browser E2E remains blocked on the Keycloak admin step
  noted in dev-setup.md.
