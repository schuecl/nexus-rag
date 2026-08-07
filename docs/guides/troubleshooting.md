# Troubleshooting

The short list of things that actually bite, each with its one-line
diagnosis and fix. Everything here was hit for real and is documented in
depth in the canonical docs (linked per entry).

## Stack won't start cleanly

??? failure "Bind-mounted configs fail with permission errors (hardened hosts)"
    **Cause:** a restrictive `umask` (e.g. `077`) makes git-created files
    mode `600`, unreadable to non-root container users.
    **Fix:** `chmod -R a+rX infra scripts` after checkout/pull.
    → [Dev environment setup](../dev-setup.md), "Restrictive host umask".

??? failure "First boot seems hung"
    **Cause:** first boot downloads ~10 GB of models before seeding.
    **Diagnosis:** `docker compose ps` — the long-running services must go
    `healthy`, then the one-shots (`ollama-model-init`, `seed-sample-data`,
    `lock-down-db-grants`) must exit `0`.
    **You're ready** when `seed-sample-data` exits 0.

## Auth and tokens

??? failure "`invalid token: Invalid issuer` when pasting a curl token into the UI"
    **Cause:** Keycloak stamps `iss` with whichever hostname the request
    used — `localhost:8080` from the host vs `keycloak:8080` from inside
    compose.
    **Fix:** already handled — both issuers are allowlisted
    (`OIDC_ISSUERS`); if you changed hostnames, extend that list.
    → [Dev environment setup](../dev-setup.md), token section.

??? failure "`Signature has expired` mid-session"
    **Cause:** dev tokens live 15 minutes.
    **Fix:** for curl, mint a new one. In LibreChat this self-heals: the MCP
    boundary returns RFC 6750 `401`, LibreChat refreshes and retries.

## LibreChat / chat plane

??? failure "OIDC login redirect fails (TLS error or unresolvable `keycloak`)"
    **Cause:** the one-time host setup was skipped.
    **Fix:** generate + trust the dev CA, add the `/etc/hosts` alias —
    both steps in [Connect the chat plane](connect-chat.md).

??? failure "The model 'sees' the rag tool but never calls it"
    **Cause:** plain chat doesn't run LibreChat's tool loop.
    **Fix:** use the **RAG Assistant Agent**
    (`scripts/create_librechat_agent.sh <user>`), endpoint selector →
    Agents. → [Connect the chat plane](connect-chat.md).

## Queries behave unexpectedly

??? failure "A document I uploaded doesn't come back in search"
    **Diagnosis, in order:** (1) is its status `approved`? Nothing else is
    retrievable — check `GET /documents/<id>` or the curation queue.
    (2) does the *querying* user's clearance/releasability/group actually
    cover the document's tags? Different users see different corpora **by
    design** — verify with a broader persona (`dave-admin`) before
    suspecting the pipeline. (3) only then check worker logs
    (`docker compose logs ingestion-worker`).

??? failure "Every query returns nothing (or only brand-new uploads), and seeding logs say `SKIP … already approved`"
    **Cause:** diverged volumes from an earlier partial run. Postgres (the
    system of record) still holds document rows from a previous stack, so
    the seeder correctly skips re-submitting them — but the Qdrant volume
    was recreated in between, so the *vectors* those rows describe no
    longer exist. Retrieval then finds nothing even though every document
    reads `approved`. Documents ingested *after* the divergence work
    normally, which makes the symptom look intermittent.
    **Confirm:** compare `select status, count(*) from documents group by
    status` in Postgres against the collection point counts in Qdrant —
    approved rows with (near-)zero points is the signature.
    **Fix:** a dev sandbox holds no data worth keeping —
    `docker compose down -v && docker compose up -d` for a coherent fresh
    seed. (This two-store split is exactly what curation's
    write-ordering protects against *within* a run; volume surgery
    between runs is outside its reach.)

??? failure "Off-topic queries return confident irrelevant results"
    **Cause:** the relevance floor (`RERANK_SCORE_FLOOR`) is unset by
    default — this is the measured abstention-noise gap.
    **Fix:** calibrate the floor per `.env.example`'s guidance; track the
    improvement with the abstention-noise metric.
    → [Evaluation & performance](../evaluation-results.md).

## Release / air-gap

??? failure "`export_release_bundle.sh` refuses, or images load with missing layers"
    **Cause:** Docker's containerd-snapshotter makes `docker save` silently
    drop layers; the script detects and refuses.
    **Fix:** switch the daemon to classic `overlay2` —
    → [Deploy air-gapped](deploy-airgapped.md), first section.

## Still stuck?

`docker compose logs --tail=100 <service>` is the ground truth. The deep
references — [Dev environment setup](../dev-setup.md) (with its full
live-validation history), [Testing](../testing.md), and
[Observability](../observability.md) — document far more edge cases than
this page curates.
