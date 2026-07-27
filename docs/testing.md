# Testing & CI Quality Gates

How this repo verifies itself: the local/CI test pyramid, what each GitHub
Actions workflow enforces, and the honest current state of each gate. Related:
[docs/dev-setup.md](dev-setup.md) for the live-stack walkthrough, and
[scripts/evaluate_retrieval.py](../scripts/evaluate_retrieval.py) for the
golden-query harness the e2e workflow reuses unchanged.

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
- **`.github/dependabot.yml`**: weekly pip (per service + test toolchain),
  GitHub Actions, and Docker base-image updates.

## Coverage policy

The enforced `--cov-fail-under=85` applies to:

- `services/common` — **excluding** `db.py`, `qdrant_store.py`, and
  `sparse_embedding.py` (see `.coveragerc`), which require live
  Postgres/Qdrant or a model download and are therefore covered by the
  compose-level e2e, not the unit gate. Everything else in `common` measures
  ~99% today.
- `app.chunking` + `app.parsing` (ingestion-worker) — the pure FR-3/FR-4
  logic. `app.processing`/`app.embedding` are pipeline glue exercised by the
  e2e job.
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

## Known gaps / follow-ups

- Wiring the trend store into CI so nightly runs retain history *across* runs
  (download the prior artifact or commit a baseline) and gate on it — the
  harness supports it (issue #71); the cross-run persistence in `e2e.yml` is the
  remaining step.
- Integration layer with containerized Postgres/Qdrant/NATS/Keycloak
  (NFR-11 crash-redelivery, NFR-13 revert-on-partial-failure, NFR-2
  append-only audit enforcement are only covered live/manually today).
- The LibreChat OIDC browser E2E remains blocked on the Keycloak admin step
  noted in dev-setup.md.
