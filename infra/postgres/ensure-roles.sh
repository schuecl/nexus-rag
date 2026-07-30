#!/bin/bash
# Issue #221: every database role, created idempotently, on every boot.
#
# These statements used to live inline in init-app-roles.sh, which the Postgres
# image runs from /docker-entrypoint-initdb.d exactly once -- on the first boot
# of a fresh data directory, never again. Any role added in a later release
# therefore silently never appeared on an existing deployment, and the failure
# surfaced layers away from its cause, as an authentication error in whatever
# service needed the role.
#
# It happened twice in two days:
#   - nexus_rag_monitor (#169): postgres-exporter reported `pg_up 0` and 0
#     pg_stat_* series against a role that did not exist.
#   - grafana_ro (#133): provision-metrics-view exited 3 with
#     `role "grafana_ro" does not exist`; the Grafana Documents panels were
#     simply empty and nothing else reported it.
#
# Documenting the caveat was not enough. Noticing it requires an operator to
# know that a new release added a role and that their volume predates it --
# neither of which is visible from the symptom.
#
# So this script is safe to re-run, and the ensure-db-roles one-shot in
# docker-compose.yml runs it on every `up`. That is the same shape
# harden-audit-log already uses for a step that cannot happen at initdb time.
#
# Roles only. Database creation stays in init-app-roles.sh: CREATE DATABASE
# cannot run inside a transaction or a DO block, and unlike a missing role it
# is not something later releases add.
#
# Passwords are (re)set on every run rather than only at creation, so rotating
# one in .env actually takes effect instead of silently disagreeing with what
# the services are configured to use -- the same class of confusing,
# far-from-the-cause failure this issue is about.
set -e

: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"

# Usable both as an initdb script (where PGHOST is a local socket) and as a
# one-shot against the running container.
PSQL="psql -v ON_ERROR_STOP=1 --username $POSTGRES_USER"

ensure_role() {
  local role="$1" password="$2"
  $PSQL --dbname postgres <<-EOSQL
	DO \$\$ BEGIN
	  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${role}') THEN
	    CREATE ROLE "${role}" WITH LOGIN PASSWORD '${password}';
	  ELSE
	    ALTER ROLE "${role}" WITH LOGIN PASSWORD '${password}';
	  END IF;
	END \$\$;
	EOSQL
}

# Issue #278 (gap G2): one role per service, not one shared application role.
#
# APP_DB_USER used to be the single identity for ingestion-api,
# ingestion-worker and orchestration-mcp, holding ALL PRIVILEGES on the whole
# database. Any one of the three -- or anything that got hold of that
# credential -- could therefore read audit_log, every document's metadata,
# oauth_states, and user_sessions (whose OIDC access/refresh tokens are stored
# in plaintext, #213). A compromise of the retrieval path also yielded *write*
# access to documents and classification_levels, tables it never touches.
#
# The grants themselves are not here. They reference tables that do not exist
# until SQLModel's create_all() has run, so they live in
# apply-service-grants.sh, which the lock-down-db-grants one-shot runs after
# ingestion-api reports healthy -- the same ordering harden-audit-log used, for
# the same reason. This file only makes the roles exist and able to connect.
ensure_role "${INGESTION_API_DB_USER:-nexus_rag_ingestion_api}" \
  "${INGESTION_API_DB_PASSWORD:-nexus_rag_ingestion_api}"
ensure_role "${INGESTION_WORKER_DB_USER:-nexus_rag_ingestion_worker}" \
  "${INGESTION_WORKER_DB_PASSWORD:-nexus_rag_ingestion_worker}"
ensure_role "${ORCHESTRATION_MCP_DB_USER:-nexus_rag_orchestration_mcp}" \
  "${ORCHESTRATION_MCP_DB_PASSWORD:-nexus_rag_orchestration_mcp}"
# The former shared role. Still created, deliberately: on an existing
# deployment it owns every table, and apply-service-grants.sh has to reassign
# that ownership away from a role it can name. It is left with no privileges at
# all -- see that script's REVOKE. Dropping it outright would strand those
# objects and break the very upgrade this change is for.
ensure_role "${APP_DB_USER:-nexus_rag_app}" "${APP_DB_PASSWORD:-nexus_rag_app}"
# Keycloak and LiteLLM each own a separate database (NFR-3).
ensure_role "${KEYCLOAK_DB_USER:-keycloak_app}" "${KEYCLOAK_DB_PASSWORD:-keycloak_app}"
ensure_role "${LITELLM_DB_USER:-litellm_app}" "${LITELLM_DB_PASSWORD:-litellm_app}"
# #133: postgres-exporter's scrape role. pg_monitor is cluster-wide read of
# every session's activity, which is why it is its own role rather than a
# grant to the application role.
ensure_role "${MONITORING_DB_USER:-nexus_rag_monitor}" "${MONITORING_DB_PASSWORD:-nexus_rag_monitor}"
# #133: read-only role for the Grafana "Documents" dashboard. The
# document_metrics view it reads is created later by provision-metrics-view --
# the `documents` table does not exist at initdb time.
ensure_role grafana_ro "${GRAFANA_DB_PASSWORD:-grafana_ro}"

# Privileges. Each of these is idempotent on its own.
#
# #278: CONNECT only. Database-wide ALL PRIVILEGES is exactly what this issue
# removes -- per-table grants are applied by apply-service-grants.sh once the
# tables exist.
$PSQL --dbname postgres <<-EOSQL
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${INGESTION_API_DB_USER:-nexus_rag_ingestion_api}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${INGESTION_WORKER_DB_USER:-nexus_rag_ingestion_worker}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${ORCHESTRATION_MCP_DB_USER:-nexus_rag_orchestration_mcp}";
	GRANT pg_monitor TO "${MONITORING_DB_USER:-nexus_rag_monitor}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${MONITORING_DB_USER:-nexus_rag_monitor}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO grafana_ro;
EOSQL

# Postgres 15+ restricts CREATE on the public schema to the database owner, and
# none of these roles own POSTGRES_DB.
#
# #278: only ingestion-api gets CREATE, and only until apply-service-grants.sh
# takes it back after startup. It is the service that runs SQLModel's
# create_all() (common/db.py's init_db(), called from its lifespan), so it needs
# CREATE on a fresh volume -- and again on any boot where a later release added
# a table. Granting it here and revoking it there gives exactly that window and
# nothing wider: this script runs before ingestion-api starts, that one runs
# after it reports healthy.
#
# The worker and the MCP server never create anything. They get USAGE only.
$PSQL --dbname "$POSTGRES_DB" <<-EOSQL
	GRANT USAGE, CREATE ON SCHEMA public TO "${INGESTION_API_DB_USER:-nexus_rag_ingestion_api}";
	GRANT USAGE ON SCHEMA public TO "${INGESTION_WORKER_DB_USER:-nexus_rag_ingestion_worker}";
	GRANT USAGE ON SCHEMA public TO "${ORCHESTRATION_MCP_DB_USER:-nexus_rag_orchestration_mcp}";
	GRANT USAGE ON SCHEMA public TO grafana_ro;
EOSQL

echo "ensure-roles: all roles present and privileges applied"
