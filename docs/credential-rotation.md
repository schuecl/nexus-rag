# Credential rotation runbook

Stage 1 of gap **G5** (`docs/roles-and-permissions.md` §7, issue #281): every
machine credential in §6's table is a long-lived static value with no
documented rotation procedure today. This document is that procedure.

**What this document does not do.** Most of these credentials still don't
support two concurrently valid values, so most rotations below remain
rotation-with-downtime: there is a window, however short, where the old value
has been replaced but not every consumer has picked up the new one, and calls
made in that window fail closed (401/403), not open. Two credentials —
the reranker shared secret and the session-token Fernet key, the two named as
concrete examples in issue #281 — now support a `_PREVIOUS` overlap value and
are called out below as no longer needing a synchronized restart. The
remaining stage-2 candidates (documented per-credential below) should land as
separate, small PRs against this list. Until a given credential has one, plan
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
| Reranker shared secret | `orchestration-mcp` → `reranker-service` | **No**, if `RERANKER_SHARED_SECRET_PREVIOUS` is used during rotation | `/rerank` 401s only if the previous-value step is skipped; `orchestration-mcp` falls back to fused order regardless, so retrieval degrades rather than fails outright (noted in the response) |
| `APP_DB_USER` Postgres password | `ingestion-api`, `orchestration-mcp` | Partial — see below | App services fail every DB query until restarted with new password |
| `SESSION_TOKEN_ENCRYPTION_KEY` (Fernet) | `ingestion-api` only | **No**, if `SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS` is used during rotation | Skipping the previous-key step makes every stored session undecryptable — forces re-login, not data loss (§5 of `roles-and-permissions.md`) |
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
(#216), and — since issue #281 gap G5 stage 2 — also against an optional
`RERANKER_SHARED_SECRET_PREVIOUS` (`app/main.py`'s `_check_shared_secret`).
This is the credential to rotate first if you want to see the stage-2 pattern
in practice: `reranker-service` is the only side with new logic; the sender
(`orchestration-mcp`) is unchanged, since it only ever presents one current
value.

No-downtime order of operations:

1. Generate a new value.
2. Set it as `RERANKER_SHARED_SECRET_PREVIOUS` on `reranker-service` — Compose:
   add it to `.env` (already wired as a passthrough); Helm: point
   `rerankerService.sharedSecret.previousExistingSecret`/`previousSecretKey`
   at a `Secret` containing the *current* (about-to-be-replaced) value. Note
   this key is unset (`""`) by default in the chart, same as
   `sharedSecret.existingSecret` — meaning `/rerank` runs unauthenticated
   until a deployment opts in; if that's your starting state, "rotation"
   here is "turning auth on for the first time," and step 2 doesn't apply.
3. Restart `reranker-service`. It now accepts both the old and new values.
4. Update `RERANKER_SHARED_SECRET` (both services, same as before) to the new
   value and restart `orchestration-mcp`. There is no ordering constraint
   between this step and step 3 completing everywhere, since
   `reranker-service` already accepts the new value from step 2 onward.
5. Once every `orchestration-mcp` replica is confirmed on the new value,
   unset `RERANKER_SHARED_SECRET_PREVIOUS` on `reranker-service` and restart
   it again to retire the old value.

Blast radius: `/rerank` only 401s if step 2 is skipped (rotating
`RERANKER_SHARED_SECRET` directly, the old single-value procedure) — and even
then, `orchestration-mcp`'s own fallback (fused RRF order, noted in the
response per `app/reranking.py`) means retrieval degrades in ranking quality
rather than failing outright.

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
(`services/common/common/token_crypto.py`, #213). Since issue #281 gap G5
stage 2, `_fernet()` builds a `cryptography.fernet.MultiFernet` from the
primary key plus an optional `SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS`: new
writes always encrypt under the primary key, and decrypts try the primary
key first, then the previous one. Rotation is no longer destructive as long
as the previous key is set through the overlap window.

There's deliberately no separate re-encryption job: `_refresh_session`
(`app/deps.py`) already rewrites `access_token`/`refresh_token`/`id_token`
on every silent token refresh, which re-encrypts that row under the primary
key as a side effect of the normal write path. Combined with `UserSession`
rows being ephemeral and bounded by `SESSION_LIFETIME`, every row is off the
previous key within one `SESSION_LIFETIME` of the rotation even if you never
write to it directly.

No-downtime order of operations:

1. Generate a new key: `python -c "from cryptography.fernet import Fernet;
   print(Fernet.generate_key().decode())"` (same method `.env.example`
   documents for the dev default).
2. Set the *current* key as `SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS` — Compose:
   add it to `.env` (already wired as a passthrough); Helm: point
   `ingestionApi.sessionTokenEncryption.previousExistingSecret`/
   `previousSecretKey` at a `Secret` containing it.
3. Set `SESSION_TOKEN_ENCRYPTION_KEY` to the new value (same field as
   before).
4. Restart `ingestion-api` once, with both env vars set from steps 2–3
   together — unlike the reranker secret, there's only one consumer here, so
   there's no benefit to splitting this into two restarts. Existing sessions
   keep decrypting via the previous key; new writes and any row touched by a
   refresh move to the new key immediately.
5. After at least one `SESSION_LIFETIME` (8 hours, `app/deps.py`) has
   elapsed since step 4, every row created before the rotation has expired.
   Unset `SESSION_TOKEN_ENCRYPTION_KEY_PREVIOUS` and restart `ingestion-api`
   again to retire the old key.

Blast radius of skipping step 2 (setting only the new
`SESSION_TOKEN_ENCRYPTION_KEY` and restarting, the old single-value
procedure): every existing `UserSession` row becomes immediately
undecryptable — forces re-login for every active session, not data loss
(rows are ephemeral, §5 of `roles-and-permissions.md`), but avoidable now
that step 2 exists.

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
- **Dual-value / no-downtime rotation for the remaining credentials** — issue
  #281's stage 2 is done for the reranker shared secret and the session-token
  Fernet key (both above); Qdrant/NATS/Keycloak rotation stays config-level
  on their side per the issue's own scoping (no code change proposed for
  them), and `APP_DB_USER` remains a candidate not yet picked up.
