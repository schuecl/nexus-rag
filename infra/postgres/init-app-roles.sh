#!/bin/bash
# NFR-3: the RAG application and Keycloak must not share a database or
# credentials, in every environment including local dev. This runs once,
# automatically, on the postgres container's first boot (Postgres's official
# image executes every /docker-entrypoint-initdb.d/* script against a fresh
# data directory only -- never again on restart) -- see docker-compose.yml's
# postgres service for the env vars this reads.
#
# Creates three non-superuser roles distinct from the bootstrap POSTGRES_USER
# (which stays superuser, used only for this script and the harden-audit-log
# one-shot service below -- never for day-to-day app, Keycloak, or LiteLLM
# traffic):
#   - APP_DB_USER: what ingestion-api/orchestration-mcp's DATABASE_URL uses.
#     Granted full privileges on the existing POSTGRES_DB database (it still
#     owns whatever tables SQLModel's create_all() creates under it -- see
#     harden-audit-log for the one exception, audit_log, locked down after
#     those tables exist).
#   - KEYCLOAK_DB_USER: owns its own separate KEYCLOAK_DB_NAME database,
#     entirely distinct from POSTGRES_DB -- Keycloak never touches app
#     tables, and the app never touches Keycloak's.
#   - LITELLM_DB_USER: owns its own separate LITELLM_DB_NAME database, same
#     reasoning -- LiteLLM's Prisma migrations (virtual keys, spend tracking)
#     stay isolated from both the app's and Keycloak's tables.
#   - MONITORING_DB_USER: what postgres-exporter scrapes as under the opt-in
#     observability profile (#133). pg_monitor + CONNECT only -- no table
#     privileges, and deliberately not APP_DB_USER.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
    CREATE ROLE "$APP_DB_USER" WITH LOGIN PASSWORD '$APP_DB_PASSWORD';
    GRANT ALL PRIVILEGES ON DATABASE "$POSTGRES_DB" TO "$APP_DB_USER";

    CREATE ROLE "$KEYCLOAK_DB_USER" WITH LOGIN PASSWORD '$KEYCLOAK_DB_PASSWORD';
    CREATE DATABASE "$KEYCLOAK_DB_NAME" OWNER "$KEYCLOAK_DB_USER";

    CREATE ROLE "$LITELLM_DB_USER" WITH LOGIN PASSWORD '$LITELLM_DB_PASSWORD';
    CREATE DATABASE "$LITELLM_DB_NAME" OWNER "$LITELLM_DB_USER";

    -- #133: the role postgres-exporter scrapes with, when the opt-in
    -- observability profile is running. Deliberately its own role rather than
    -- APP_DB_USER: the exporter's collectors need pg_monitor (pg_stat_*,
    -- pg_read_all_stats) to return anything at all, and pg_monitor is
    -- cluster-wide read of every session's activity -- not something the
    -- credential ingestion-api and orchestration-mcp run as should carry.
    -- CONNECT only, no table privileges: it reads statistics views, never
    -- corpus data.
    CREATE ROLE "$MONITORING_DB_USER" WITH LOGIN PASSWORD '$MONITORING_DB_PASSWORD';
    GRANT pg_monitor TO "$MONITORING_DB_USER";
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO "$MONITORING_DB_USER";
EOSQL

# Postgres 15+ restricts CREATE on the public schema to the database owner by
# default -- APP_DB_USER isn't the owner of POSTGRES_DB (POSTGRES_USER still
# is), so without this, SQLModel's create_all() would fail the first time
# ingestion-api starts up and tries to create its tables.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    GRANT ALL ON SCHEMA public TO "$APP_DB_USER";
EOSQL

# #133: metadata-only view + scoped read-only role for the Grafana "Documents"
# dashboard. grafana_ro can SELECT this view and nothing else -- it exposes
# governance metadata (classification/doc_type/status/org/timestamps) but NOT
# filenames (content, per the purge path) or the base tables.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE OR REPLACE VIEW document_metrics AS
      SELECT classification, doc_type, status, owner_org, created_at, updated_at
      FROM documents;
    DO \$\$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro LOGIN PASSWORD '${GRAFANA_DB_PASSWORD:-grafana_ro}';
      END IF;
    END \$\$;
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO grafana_ro;
    GRANT USAGE ON SCHEMA public TO grafana_ro;
    GRANT SELECT ON document_metrics TO grafana_ro;
EOSQL
