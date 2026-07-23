#!/bin/bash
# NFR-3: the RAG application and Keycloak must not share a database or
# credentials, in every environment including local dev. This runs once,
# automatically, on the postgres container's first boot (Postgres's official
# image executes every /docker-entrypoint-initdb.d/* script against a fresh
# data directory only -- never again on restart) -- see docker-compose.yml's
# postgres service for the env vars this reads.
#
# Creates two non-superuser roles distinct from the bootstrap POSTGRES_USER
# (which stays superuser, used only for this script and the harden-audit-log
# one-shot service below -- never for day-to-day app or Keycloak traffic):
#   - APP_DB_USER: what ingestion-api/orchestration-mcp's DATABASE_URL uses.
#     Granted full privileges on the existing POSTGRES_DB database (it still
#     owns whatever tables SQLModel's create_all() creates under it -- see
#     harden-audit-log for the one exception, audit_log, locked down after
#     those tables exist).
#   - KEYCLOAK_DB_USER: owns its own separate KEYCLOAK_DB_NAME database,
#     entirely distinct from POSTGRES_DB -- Keycloak never touches app
#     tables, and the app never touches Keycloak's.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
    CREATE ROLE "$APP_DB_USER" WITH LOGIN PASSWORD '$APP_DB_PASSWORD';
    GRANT ALL PRIVILEGES ON DATABASE "$POSTGRES_DB" TO "$APP_DB_USER";

    CREATE ROLE "$KEYCLOAK_DB_USER" WITH LOGIN PASSWORD '$KEYCLOAK_DB_PASSWORD';
    CREATE DATABASE "$KEYCLOAK_DB_NAME" OWNER "$KEYCLOAK_DB_USER";
EOSQL

# Postgres 15+ restricts CREATE on the public schema to the database owner by
# default -- APP_DB_USER isn't the owner of POSTGRES_DB (POSTGRES_USER still
# is), so without this, SQLModel's create_all() would fail the first time
# ingestion-api starts up and tries to create its tables.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    GRANT ALL ON SCHEMA public TO "$APP_DB_USER";
EOSQL
