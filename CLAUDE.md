# CLAUDE.md

# Token efficiency
Respond like smart caveman. Cut all filler, keep technical substance.
- Drop articles (a, an, the), filler (just, really, basically, actually).
- Drop pleasantries (sure, certainly, happy to).
- No hedging. Fragments fine. Short synonyms.
- Technical terms stay exact. Code blocks unchanged.
- Pattern: [thing] [action] [reason]. [next step].

## What this is

Air-gapped Retrieval-Augmented Generation (RAG) pipeline for **MPNexus**, a DoD Kubernetes
environment already running LibreChat, LiteLLM, Keycloak, and vLLM/Ollama. This repo adds
document ingestion, mandatory classification/releasability tagging, curator review, and
claims-based access-controlled retrieval on top of that stack, exposed to LibreChat as a
custom MCP tool.

`REQUIREMENTS.md` is the source of truth for scope — every FR-*/NFR-* referenced in code
comments and commit messages traces back to it. `ARCHITECTURE.md` has component diagrams,
the data model, and sequence diagrams per major flow (ingestion, curation, retrieval,
UI login, supersession) — read it before touching any of those flows. `docs/governance.md`
covers corpus governance (access control, curation as the data-quality gate, lineage,
retention gaps).

**Core security invariant:** every Classification/Releasability/Access-scope decision (what
a user may tag at upload, what a curator may approve, what a user may see at query time) is
derived server-side from verified OIDC claims (`clearance`, `releasability`, `groups`, `org`,
`rag_roles`) through one shared library (`services/common/common/claims.py`), never trusted
from client input. Qdrant's own RBAC is coarse-grained only — real enforcement is a mandatory
payload filter built from those claims and applied to *both* the dense and BM25 legs of
hybrid retrieval, so neither can bypass it.

## Repo layout

```
services/
  common/               # shared claims/metadata/Qdrant-filter/object-store/job-queue library
  ingestion-api/         # upload + curation UI/API (FastAPI + Jinja2/HTMX)
  ingestion-worker/       # durable parse/chunk/embed/store pipeline (NATS JetStream consumer)
  orchestration-mcp/      # retrieval MCP server (FastMCP) exposing rag_search
  reranker-service/       # cross-encoder reranking API
infra/
  keycloak/realm-export/  # seeded dev realm (claims schema, org curator roles, test users)
  librechat/, litellm/    # throwaway dev configs for the MCP/OBO + generation path
scripts/                  # sample-data seeding, golden-query retrieval evaluation harness
helm/nexus-rag/           # production Helm chart
docs/                     # dev-setup.md, testing.md, governance.md
```

Every service ships a top-level `app` package with the same name — this is why they're
never pytest-collected together (see Testing below) and why each has its own
`tests/conftest.py` sys.path shim.

## Commands

### Setup
```bash
python3 -m venv .venv-test && source .venv-test/bin/activate
pip install -r requirements-test.txt
pip install "pydantic>=2.7" "PyJWT>=2.8" "cryptography>=42.0" \
  "sqlmodel>=0.0.21" "qdrant-client>=1.10" "nats-py>=2.7" "boto3>=1.34"
```

### Tests
```bash
# Repo-root tree: services/common unit tests + BDD security scenarios + scripts/ tests
# (this is what ci.yml's `unit` job runs, on Python 3.11 and 3.12)
pytest tests/unit tests/e2e --cov=common --cov-branch --cov-fail-under=85

# A single test file/case
pytest tests/unit/common/test_purge.py -v
pytest tests/unit/common/test_purge.py::TestIdempotenceAndErrors::test_unknown_document_raises_not_found -v

# Per-service suites -- MUST run one service at a time, from its own directory
# (running two together fails: the second's `from app...` resolves against the
# first's already-imported `app` package). `pytest` at the repo root does NOT
# reach these.
(cd services/ingestion-api && pytest tests -q)
(cd services/ingestion-worker && pytest tests -q --cov=app.chunking --cov=app.parsing --cov-fail-under=85)
(cd services/orchestration-mcp && pytest tests -q --cov=app.reranking --cov-fail-under=85)

# Full-stack e2e (identical to e2e.yml's golden-query job)
docker compose up -d --wait
docker compose --profile eval run --rm eval-retrieval
```

### Lint / types / security
```bash
ruff check services scripts tests       # lint gate (line-length 100)
ruff format --check services scripts tests  # format gate (issue #80)
mypy services/common/common             # type gate -- enforced across services/common and
                                         # all four app services (see ci.yml's `types` job)
python scripts/check_pinned_images.py   # NFR-16: no floating/`:latest` image or model tags
bandit -r services scripts --exclude '*/tests/*'
helm lint helm/nexus-rag
```

### Run the dev stack
```bash
cp .env.example .env
docker compose up --build
```
First boot pulls Ollama models and HF reranker/BM25 caches (~10GB, needs internet once),
then `seed-sample-data` submits and curates 7 sample documents automatically. See
`docs/dev-setup.md` for seeded Keycloak users (`alice-ingest`, `bob-query`,
`carol-curator`, `dave-admin`; password `devpass123`), service URLs/ports, and a full
ingest → curate → query walkthrough — including one-time host setup (trusted dev CA,
`/etc/hosts` entry) required for LibreChat's OIDC login to work at all.

## Architecture

**Ingestion:** browser upload through `ingestion-api` → tagging validated against claims
(FR-18, synchronous) → original stored durably (object store, NFR-12) → Postgres row
`queued` → job published to NATS JetStream (NFR-11) → `ingestion-api` returns 202
immediately → `ingestion-worker` (separate service, durable consumer) parses/chunks/embeds
→ writes chunk vectors to Qdrant tagged `pending_review` → curator approves/rejects/corrects
→ approval flips chunks to `approved`, which is what makes them retrievable. The worker
only acks a JetStream message on a terminal outcome (success or a permanent parse/embed
failure → `failed`); a transient error (DB/Qdrant unreachable) is left un-acked so
JetStream redelivers it — this is what makes ingestion crash-safe (NFR-11).

**Retrieval:** LibreChat calls `orchestration-mcp`'s `rag_search` MCP tool over streamable
HTTP, forwarding the user's identity in the Authorization header → claims parsed, a
mandatory access filter built server-side → dense (Ollama embeddings) and BM25 sparse legs
query Qdrant in parallel with that filter applied to both, fused via Reciprocal Rank Fusion
→ fused candidates reranked by `reranker-service` (falls back to fused order, noted in the
response, if unreachable) → results with source/classification metadata returned. Every
query is audit-logged by OIDC identity, whether it succeeded, was denied, or hit an
unreachable Qdrant. `orchestration-mcp` also exposes the same logic as
`POST /debug/rag_search` for curl-based testing without an MCP client.

**Curation (NFR-13):** on approve/reject, the Qdrant payload write happens *before* the
Postgres commit (so a curator can retry through the same API call if the commit fails —
`_load_pending` only accepts a document still `pending_review`). If something between the
Qdrant write and the commit raises, `approve()`/`reject()` best-effort revert the Qdrant
payload back to `pending_review` before re-raising, so the two stores can't end up
disagreeing about a document's status.

**Supersession (FR-7):** the *new* document's chunks flip to `approved` before the *old*
document's chunks are deleted — never a window where neither version is retrievable.
Curator authority is re-checked against the *old* document too, since a new version can
change classification.

**Data model:** Postgres is the transactional system of record (`documents.status`:
`queued|processing|embedded|pending_review|approved|rejected|superseded|failed`,
`audit_log`, `notifications`, admin-configurable `classification_levels`/
`releasability_values`). Qdrant holds the actual chunk vectors — one point per chunk, two
named vectors (`dense`, `bm25`) — plus a copy of the access-control payload fields
(`status`, `classification`, `releasability`, `access_scope`), so retrieval filters without
a round trip to Postgres. The object store (NFR-12) holds original uploaded bytes, keyed
independently of both.

## Testing structure (why it looks the way it does)

- **Two separate pytest trees, deliberately not merged.** `tests/unit/common/` (repo root)
  covers `services/common` — the package with no service directory of its own — plus
  `tests/e2e/features/*.feature` (BDD access-control scenarios) and `tests/unit/` scripts
  tests (e.g. `test_evaluate_retrieval.py`). `services/<name>/tests/` covers each app
  service's own code. They cannot be collected in one pytest process: every service ships
  a top-level `app` package, so collecting two services together resolves `app` to
  whichever landed on `sys.path` first (issue #113).
- **No live infra for unit/BDD.** JWTs are real RS256 tokens minted against an in-memory
  keypair (`tests/conftest.py`), verified through the production `parse_claims` path with a
  stubbed JWKS client. Classification ranking runs against in-memory SQLite.
- **Coverage gate (`--cov-fail-under=85`) is scoped, not repo-wide**: `services/common`
  (excluding `db.py`/`qdrant_store.py`/`sparse_embedding.py` — need live Postgres/Qdrant or
  a model download, see `.coveragerc`), `app.chunking`+`app.parsing`+`app.ocr` in
  ingestion-worker (the last via that service's `pyproject.toml` `addopts`, since
  ci.yml's matrix string is the required check's name — see `docs/testing.md`),
  `app.reranking` in orchestration-mcp. `ingestion-api`'s route layer and
  `orchestration-mcp/app/rag_search.py` are measured but not gated — they need a
  containerized integration layer to test meaningfully (open gap, not hidden).
- **Mutation testing is advisory only** (`e2e.yml`, nightly, `continue-on-error: true`)
  against `claims.py`/`qdrant_filters.py`/`metadata.py`/`versioning.py` — the modules where
  a subtle logic bug is a security incident. Runs from `services/common` specifically
  (`cd services/common && mutmut run`); running from repo root breaks module-name matching.
  `services/common` must not be pip-installed in that job or the installed copy shadows the
  mutated one.
- **The golden-query e2e job is byte-identical to a local run**: same `docker compose up`,
  same `docker compose --profile eval run --rm eval-retrieval`. It fails on any recall miss
  against `scripts/golden_queries.json` and on any forbidden (pending/rejected/superseded)
  document leaking into results regardless of querying persona — a security regression
  (FR-26), not just a quality miss. `scripts/evaluate_retrieval.py` also supports
  `--history-dir`/`--baseline`/`--regression-tolerance` to persist runs and fail on a
  recall/precision drop against a prior baseline (FR-30/FR-32) — see `docs/testing.md`.

Full detail, including the honest list of what's gated vs. advisory vs. still a gap, lives
in `docs/testing.md`.

## Conventions

- **Scope discipline.** Changes are expected to trace back to an FR-*/NFR-* in
  `REQUIREMENTS.md` or an open issue — open an issue before a non-trivial change.
- **One logical change per PR**, squashed to a single well-described commit before opening.
- **Honest confidence labeling.** Docs distinguish *implemented* (code exists, never
  executed here), *tested against mocks* (run against an in-memory/stubbed substitute), and
  *validated against a live environment* (run against the real `docker compose up` stack).
  Don't upgrade a claim past what was actually exercised — see `docs/dev-setup.md`'s
  "What's stubbed vs working" for the current convention and status of every major feature.
- **ruff is the lint/import-order/format arbiter** (`known-first-party = ["common", "app"]`,
  100-column lines); both `ruff check` and `ruff format --check` are enforced (issue #80).
- **mypy is a hard gate on `services/common` and all four app services** (issue #79);
  each app service scopes `disallow_untyped_defs` to its own `app.*` via a
  `pyproject.toml` override, since `MYPYPATH=../common` also pulls
  `services/common` into that run and its settings must not tighten as a
  side effect.
- Tests for `common` go under `tests/unit/common/` (root) so the coverage gate sees them;
  per-service tests go under `services/<service>/tests/`.
