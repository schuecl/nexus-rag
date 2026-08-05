# Contributing to nexus-rag

Thanks for your interest in improving nexus-rag. This project is scope-driven:
`REQUIREMENTS.md` is the source of truth, and changes are expected to trace back
to a functional (FR-*) or non-functional (NFR-*) requirement, or to an open
issue.

## Workflow

1. **Open an issue first.** Describe the change and, where relevant, the FR/NFR
   or existing issue it addresses. This keeps design discussion out of the diff
   and avoids duplicated work. For security-sensitive reports, do **not** open a
   public issue — follow [SECURITY.md](SECURITY.md) instead.
2. **Branch.** Base your work on the latest `main` and use a descriptive branch
   name, e.g. `fix/123-purge-path`, `feat/72-observability`, `docs/135-repo-presentation`.
3. **Keep the change focused.** One logical change per pull request. Squash your
   branch into a single, well-described commit before opening the PR.
4. **Open a pull request** against `main`, referencing the issue it closes
   (`Closes #NNN`). Fill in the PR template checklist.
5. **Green CI is required.** All the gates below must pass; a maintainer reviews
   and merges.

## Running the checks locally

The full test strategy — the unit/BDD/e2e pyramid, the per-service coverage
gates, and the honest coverage gaps — is documented in
[docs/testing.md](docs/testing.md). In short:

```bash
# Unit + BDD security scenarios (services/common), with the coverage floor CI enforces
pytest tests/unit/common tests/e2e --cov=common --cov-branch --cov-fail-under=85

# Per-service suites run one service at a time (each ships a top-level `app` package)
cd services/ingestion-worker && pytest tests -q --cov=app.chunking --cov=app.parsing
cd services/orchestration-mcp && pytest tests -q --cov=app.reranking

# Lint and types (matching the CI versions)
ruff check services scripts tests
mypy services/common/common
```

For an end-to-end run against the real stack, see
[docs/dev-setup.md](docs/dev-setup.md) (`docker compose up`, seeded Keycloak
users, and a full walkthrough).

## CI gates a pull request must pass

These run automatically on every PR (see `.github/workflows/`, added in #67):

- **`ci.yml`** — unit + BDD with an enforced ≥85% line+branch coverage floor
  (scoped per `docs/testing.md`), per-service test suites, `ruff`, `mypy` on
  `services/common` (report-only on the app services), the NFR-16 floating-tag
  check, and a full `docker compose build`.
- **`security.yml`** — `bandit`, `pip-audit` on the shipped dependency tree,
  `helm lint`/`template`, a Trivy filesystem scan (results uploaded to the
  Security tab), and a `gitleaks` secret scan.
- **CodeQL** — required for merge.

**Not required on every PR:** `e2e.yml`'s golden-query job — full
`docker compose up` → seed → golden-query retrieval evaluation, failing on
any recall miss or any forbidden (pending/rejected/superseded) document
leaking into results — and its `integration` job (#428) — `tests/integration/`
against a live Postgres, bootstrapped the same way the dev stack's own
one-shots bootstrap it, currently covering NFR-2's audit-log append-only
enforcement. Both run nightly and on manual dispatch regardless; add the
`needs-e2e` label to a PR to also run them there when a change is genuinely
retrieval/ingestion-risky, or touches database roles/grants (see
`docs/testing.md` for why neither is a required check).

## Coding conventions

- Match the style of the surrounding code; `ruff` (pinned in CI) is the arbiter
  for lint and import ordering.
- Prefer adding regression coverage next to the behavior it protects. Tests for
  the shared `common` package live under `tests/unit/common/` so the coverage
  gate sees them; per-service tests live under `services/<service>/tests/`.
- Keep the docs honest: this project labels claims as *implemented*,
  *tested against mocks*, or *validated live* (see `docs/dev-setup.md`). Don't
  upgrade a label past what you actually exercised.

## Reporting bugs and requesting features

Use the issue templates under [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE).
By contributing, you agree that your contributions will be licensed under the
project's [Apache-2.0 License](LICENSE), and you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).
