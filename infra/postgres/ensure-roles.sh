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

# The application role: ingestion-api and orchestration-mcp connect as this.
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
$PSQL --dbname postgres <<-EOSQL
	GRANT ALL PRIVILEGES ON DATABASE "${POSTGRES_DB}" TO "${APP_DB_USER:-nexus_rag_app}";
	GRANT pg_monitor TO "${MONITORING_DB_USER:-nexus_rag_monitor}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${MONITORING_DB_USER:-nexus_rag_monitor}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO grafana_ro;
EOSQL

# Postgres 15+ restricts CREATE on the public schema to the database owner, and
# APP_DB_USER is not the owner of POSTGRES_DB -- without this, SQLModel's
# create_all() fails the first time ingestion-api starts.
$PSQL --dbname "$POSTGRES_DB" <<-EOSQL
	GRANT ALL ON SCHEMA public TO "${APP_DB_USER:-nexus_rag_app}";
	GRANT USAGE ON SCHEMA public TO grafana_ro;
EOSQL

echo "ensure-roles: all roles present and privileges applied"
