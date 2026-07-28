#!/bin/bash
# NFR-3: the RAG application and Keycloak must not share a database or
# credentials, in every environment including local dev. This runs once,
# automatically, on the postgres container's first boot (Postgres's official
# image executes every /docker-entrypoint-initdb.d/* script against a fresh
# data directory only -- never again on restart) -- see docker-compose.yml's
# postgres service for the env vars this reads.
#
# Since #221 this file creates *databases* only. Role creation moved to
# ensure-roles.sh, which is idempotent and re-run on every `up` by the
# ensure-db-roles one-shot -- because "runs once, on a fresh data directory"
# meant every role added by a later release silently never appeared on an
# existing deployment. That happened twice: nexus_rag_monitor (#169) and
# grafana_ro (#133).
#
# Databases stay here. CREATE DATABASE cannot run inside a transaction or a DO
# block, so it cannot be made idempotent the same way -- and unlike roles, it
# is not something later releases add.
#
# Databases created:
#   - KEYCLOAK_DB_NAME, owned by KEYCLOAK_DB_USER -- entirely distinct from
#     POSTGRES_DB. Keycloak never touches app tables, and the app never
#     touches Keycloak's.
#   - LITELLM_DB_NAME, owned by LITELLM_DB_USER -- same reasoning; LiteLLM's
#     Prisma migrations (virtual keys, spend tracking) stay isolated from both.
#
# POSTGRES_DB itself is created by the image before this script runs, and stays
# owned by the bootstrap POSTGRES_USER -- a superuser used only by this script,
# ensure-roles.sh, and the harden-audit-log one-shot, never for day-to-day app,
# Keycloak, or LiteLLM traffic.
set -e

# Roles first: the databases below are created OWNER <role>, so those roles
# have to exist. Same script the ensure-db-roles one-shot re-runs every boot.
/ensure-roles.sh

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
	CREATE DATABASE "${KEYCLOAK_DB_NAME:-keycloak}" OWNER "${KEYCLOAK_DB_USER:-keycloak_app}";
	CREATE DATABASE "${LITELLM_DB_NAME:-litellm}" OWNER "${LITELLM_DB_USER:-litellm_app}";
EOSQL
