# Credential rotation runbook

Stage 1 of gap **G5** (`docs/roles-and-permissions.md` §7, issue #281): every
machine credential in §6's table is a long-lived static value with no
documented rotation procedure today. This document is that procedure.

**What this document does not do.** None of these credentials currently
support two concurrently valid values, so every rotation below is
rotation-with-downtime: there is a window, however short, where the old value
has been replaced but not every consumer has picked up the new one, and calls
made in that window fail closed (401/403), not open. Stage 2 (dual-value
support — e.g. a reranker `RERANKER_SECRET_PREVIOUS`, `MultiFernet` for the
session key) removes that window per-credential and should land as separate,
small PRs against this list. Until a given credential has a stage-2 PR, plan
rotation for a maintenance window sized to "restart every consumer."

Compose commands below assume `.env` at the repo root (dev stack). Helm
commands assume the `existingSecret` pattern documented in
`helm/nexus-rag/values.yaml` — you update the pre-created Kubernetes `Secret`
out-of-band (your cluster's secrets process, e.g. `kubectl create secret
generic ... --dry-run=client -o yaml | kubectl apply -f -` or your sealed-
secrets/external-secrets tooling), then roll the Deployments/StatefulSets
that mount it.

## Summary

| Credential | Consumers | Downtime today | Blast radius if skipped/wrong order |
|---|---|---|---|
| Qdrant read/write API key | `ingestion-api`, `ingestion-worker` → Qdrant | Yes — Qdrant restart | Writes/curation actions 401 until restarted with new key |
| Qdrant read-only API key | `orchestration-mcp` → Qdrant; Prometheus scrape (dev, `--profile observability`) | Yes — Qdrant restart | Queries 401 (fails closed — no results, not unfiltered results); scrape 401s silently (#133) if forgotten |
| NATS account passwords | `ingestion-api` (publish), `ingestion-worker` (consume) | Yes — NATS restart | Publish/consume auth failures; JetStream redelivery means no message loss, just a stall (NFR-11) |
| Reranker shared secret | `orchestration-mcp` → `reranker-service` | Yes — both restart | `/rerank` 401s; `orchestration-mcp` falls back to fused order per its own design, so retrieval degrades rather than fails (noted in the response) |
| `APP_DB_USER` Postgres password | `ingestion-api`, `orchestration-mcp` | Partial — see below | App services fail every DB query until restarted with new password |
| `SESSION_TOKEN_ENCRYPTION_KEY` (Fernet) | `ingestion-api` only | Yes, and **destructive** | Every stored session becomes undecryptable — forces re-login, not data loss (§5 of `roles-and-permissions.md`) |
| Keycloak client secret (`rag-app`) | `ingestion-api` OIDC token exchange | Yes — `ingestion-api` restart | Login/token-refresh calls to Keycloak fail until restarted with new secret |

## Qdrant API keys

Both keys (`qdrant.apiKey.secretKey` = read/write, `qdrant.apiKey.
readOnlySecretKey` = read-only) are issued together in one `Secret` (`nexus-
rag-qdrant-keys` in the chart's default values) and Qdrant itself reads them
at its own process startup (`QDRANT__SERVICE__API_KEY` /
`QDRANT__SERVICE__READ_ONLY_API_KEY` — `helm/nexus-rag/templates/qdrant-
statefulset.yaml`, `docker-compose.yml`'s `qdrant` service). There is no
online-rotation API on the Qdrant side for this deployment's config —
changing either key requires restarting the Qdrant process itself, which is
why this is the one credential in the table where *the credential holder*,
not just its consumers, has a restart in the critical path.

Order of operations:

1. Generate new values for both keys (they don't have to change together,
   but they're issued together in this repo's convention — see the
   `values.yaml` comment on why one `Secret`/two keys rather than two
   `Secret`s).
2. Compose: update `QDRANT_API_KEY`/`QDRANT_READ_ONLY_API_KEY` in `.env`.
   Helm: update the `nexus-rag-qdrant-keys` `Secret` in place.
3. Restart Qdrant (`docker compose up -d qdrant`, or roll the Qdrant
   StatefulSet). Until this happens, every consumer's *new* key would be
   rejected by the *old*-configured Qdrant — do this before step 4, not
   after.
4. Restart `ingestion-api`, `ingestion-worker` (read/write key), and
   `orchestration-mcp` (read-only key). If running the observability profile,
   also restart `prometheus` — it renders `QDRANT_READ_ONLY_API_KEY` into a
   credentials file at its own container start (`docker-compose.yml`'s
   `prometheus` entrypoint), and issue #133 is exactly this failure mode: a
   stale scrape credential 401s silently, with nothing surfacing the cause
   beyond an empty Qdrant dashboard panel.

Blast radius of doing steps 3/4 out of order, or skipping 4 for one service:
that service's Qdrant calls 401 until it's restarted. Because the FR-26
retrieval filter fails closed, a stale `orchestration-mcp` key produces empty
query results, not unfiltered ones — the failure is loud (users see "no
results" or a 5xx) rather than a silent access-control gap.

## NATS account passwords

`ingestion-api` (publish-only) and `ingestion-worker` (consume-only) each
authenticate as their own user against `infra/nats/nats.conf`'s per-subject
permission blocks (#212). NATS reads `$NATS_INGESTION_API_PASSWORD` /
`$NATS_INGESTION_WORKER_PASSWORD` via config-file variable substitution at
its own startup, same shape as Qdrant above: the credential holder (NATS)
and both consumers all need a restart, not just the consumers.

Order of operations:

1. Compose: update `NATS_INGESTION_API_PASSWORD`/`NATS_INGESTION_WORKER_PASSWORD`
   in `.env`. Helm: update the `nexus-rag-nats-ingestion-api` and/or
   `nexus-rag-nats-ingestion-worker` `Secret`s in place.
2. Restart NATS (`docker compose up -d nats`, or roll the NATS StatefulSet).
3. Restart `ingestion-api` and/or `ingestion-worker`, whichever password(s)
   changed.

Blast radius: publish/consume auth failures for the affected service until
restarted. This is one of the lower-stakes rotations in this list —
JetStream is durable (NFR-11), so a publish failure surfaces immediately at
upload time (the user's request fails, nothing is silently lost) and a
consume failure just stalls processing until `ingestion-worker` reconnects;
no message is dropped either way.

## Reranker shared secret

`orchestration-mcp` sends `X-Reranker-Shared-Secret`; `reranker-service`
checks it with `hmac.compare_digest` against its own `RERANKER_SHARED_SECRET`
(#216). Today it's a single value on each side — no
`RERANKER_SECRET_PREVIOUS`-style overlap (that's the concrete stage-2 example
named in issue #281).

Order of operations:

1. Generate a new value.
2. Compose: update `RERANKER_SHARED_SECRET` in `.env` (read by both
   services). Helm: update the `Secret` referenced by
   `rerankerService.sharedSecret.existingSecret` — note this is unset
   (`""`) by default in the chart, meaning `/rerank` runs unauthenticated
   unless a deployment has already opted in; if that's your starting state,
   "rotation" here is really "turning auth on for the first time," and both
   services need it set to the *same* Secret.
3. Restart `reranker-service` and `orchestration-mcp` together. Order
   between the two doesn't matter — whichever restarts first, the other is
   still presenting/checking the old value for a brief window, and that
   direction fails closed (401), not open.

Blast radius: `/rerank` calls 401 during the window. `orchestration-mcp`'s
own fallback (fused RRF order, noted in the response per
`app/reranking.py`) means retrieval degrades in ranking quality rather than
failing outright — the one rotation in this table where a mismatch doesn't
break the user-facing feature, just its quality, and says so in the response
it returns.

## `APP_DB_USER` Postgres password

`infra/postgres/ensure-roles.sh` runs on every `docker compose up` (not just
first boot — #221) and unconditionally `ALTER ROLE`s every managed role,
including `APP_DB_USER`, to whatever password is currently in the
environment. This is the "partial bright spot" issue #281 calls out: the
database side of rotation already happens automatically. What it doesn't do
is restart the services that hold a connection pool opened with the old
password.

Order of operations:

1. Compose: update `APP_DB_PASSWORD` in `.env`, then `docker compose up -d`
   (or at minimum re-run the `ensure-db-roles` one-shot) so `ensure-roles.sh`
   applies it. Helm: update the `nexus-rag-db` `Secret`'s `database-url` key
   (`externalPostgres.existingSecret`/`secretKey`) — Postgres itself is
   external to the chart in production, so applying the new password
   Postgres-side is your own Postgres deployment's process, not something
   this repo automates the way `ensure-roles.sh` does for the dev stack.
2. Restart `ingestion-api` and `orchestration-mcp` so they reconnect with the
   new password. Existing pooled connections opened under the old password
   keep working until they're recycled or the service restarts — this is
   the "rotation-with-downtime" issue #281 flags, since nothing forces that
   recycle.

Blast radius of restarting the app services *before* the DB has the new
password (reversed order): every new connection attempt fails auth
immediately — worse than the correct order's brief pool-staleness window,
since it's a hard outage instead of old-connections-still-working.

## `SESSION_TOKEN_ENCRYPTION_KEY` (Fernet key)

Encrypts `UserSession`'s stored OIDC access/refresh/id tokens at rest
(`services/common/common/token_crypto.py`, #213). This is the one rotation
in this table that is not just "downtime," it's **destructive**: the module
uses a single `Fernet(key)`, not `cryptography.fernet.MultiFernet`, so there
is no code path today that can decrypt a row written under the old key once
the env var changes. Every stored session becomes permanently unreadable the
moment the new key is live.

This is deliberately a lower-severity kind of destructive than losing a key
over durable document content — `UserSession` rows are ephemeral and bounded
by `SESSION_LIFETIME` (see the comment in `token_crypto.py`), so the
consequence is every logged-in user is forced to re-authenticate, not data
loss. Still worth doing on purpose, not by accident:

1. Generate a new key: `python -c "from cryptography.fernet import Fernet;
   print(Fernet.generate_key().decode())"` (same method `.env.example`
   documents for the dev default).
2. Compose: update `SESSION_TOKEN_ENCRYPTION_KEY` in `.env`. Helm: update the
   `nexus-rag-session-token-key` `Secret`'s `key` field
   (`ingestionApi.sessionTokenEncryption.existingSecret`/`secretKey`).
3. Restart `ingestion-api`. Every existing `UserSession` row is now
   undecryptable; the next request against any of them hits a decrypt error
   and should be treated as "session invalid, re-authenticate" — there's no
   recovery path other than the user logging in again.

Stage 2 for this credential (tracked separately per issue #281's proposal)
is switching to `MultiFernet([Fernet(new), Fernet(old)])`: new writes use the
first key, reads try each key in order, and a background or on-read
re-encryption pass migrates rows off the old key before it's retired. Until
that lands, treat any rotation of this key as forcing a full re-login for
every active session, on purpose.

## Keycloak client secret (`rag-app`)

`ingestion-api` presents this as `client_secret` in its OIDC authorization-
code token exchange and refresh calls (`app/deps.py`, `app/routes/auth.py`).
Unlike the credentials above, the verifying side (Keycloak) is out of this
repo's enforcement scope (§6 of `roles-and-permissions.md`) — rotation is a
Keycloak admin action, not something this repo's config alone drives.

Order of operations:

1. In the Keycloak admin console (or `kcadm.sh`), regenerate the `rag-app`
   client's secret. Keycloak has its own credential-rotation UI for
   confidential clients; use it rather than hand-editing the realm.
2. Compose: update `RAG_APP_KEYCLOAK_CLIENT_SECRET` in `.env`. Helm: update
   the `nexus-rag-keycloak-client-secret` `Secret`'s `client-secret` field
   (`externalKeycloak.clientSecret.existingSecret`/`secretKey`).
3. Restart `ingestion-api`.

Blast radius: between steps 1 and 3, any token exchange or silent refresh
(`app/deps.py`'s `_refresh_session`) fails — users mid-login get an error,
and background refreshes for already-logged-in sessions fail until
`ingestion-api` restarts with the new secret, at which point normal refresh
resumes (this doesn't invalidate existing sessions the way the Fernet key
rotation above does — only the app↔Keycloak leg is affected, not the stored
session rows).

## What's not here

- **Qdrant/NATS/Keycloak's own credential storage** — this document covers
  updating what nexus-rag's services present to those systems, not those
  systems' internal auth stores. Regenerating them Keycloak/Qdrant/NATS-side
  is each system's own admin process.
- **Dual-value / no-downtime rotation** — issue #281's stage 2, tracked
  per-credential as its own small PR. The reranker shared secret is the
  concrete example named in the issue (`RERANKER_SECRET_PREVIOUS`); Fernet's
  `MultiFernet` is noted above as the session-key equivalent.
