#!/bin/bash
# Issue #317: the per-service GRANT matrix (#278) lives in grant-matrix.sql,
# not here, so it has exactly one source of truth but two call sites:
#
#   1. This one-shot in docker-compose.yml, which applies it alone, connected
#      as the bootstrap superuser, right after migrate-db-schema creates the
#      tables and before ingestion-api/ingestion-worker start. On a
#      brand-new volume, migrate-db-schema's init_db() now creates every
#      table owned by the bootstrap superuser (#314/#316) -- so
#      INGESTION_API_DB_USER holds no privileges on them at all until
#      something grants some, and ingestion-api's lifespan (_seed_defaults,
#      main.py) needs SELECT+INSERT on classification_levels before it can
#      report healthy. The one-shot that used to grant that
#      (lock-down-db-grants/apply-service-grants.sh) only runs *after*
#      ingestion-api is healthy -- a cycle neither side can break on its own.
#      Applying the matrix here first closes that window.
#   2. apply-service-grants.sh, which \i's grant-matrix.sql directly inside
#      its own explicit transaction after its REVOKE ALL pass (#319), so
#      lock-down-db-grants remains the authority that also strips anything
#      this early pass didn't need to remove (nothing, on a fresh volume; a
#      stale grant from a prior release, on an upgrade) -- and so REVOKE and
#      re-GRANT commit together rather than as separate transactions.
#
# Granting does not require table ownership -- POSTGRES_USER is the bootstrap
# superuser, which can GRANT on any table regardless of who owns it -- so this
# is safe to run before ownership has ever been reassigned.
set -e

: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"

API_ROLE="${INGESTION_API_DB_USER:-nexus_rag_ingestion_api}"
WORKER_ROLE="${INGESTION_WORKER_DB_USER:-nexus_rag_ingestion_worker}"
MCP_ROLE="${ORCHESTRATION_MCP_DB_USER:-nexus_rag_orchestration_mcp}"
# Issue #309: not put through the REVOKE-ALL-then-regrant loop in
# apply-service-grants.sh -- it never holds anything to be reassigned or
# stripped, so it only needs its one SELECT grant (re)applied here.
REPORTING_ROLE="${AUDIT_REPORTING_DB_USER:-nexus_rag_audit_reporting}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v api_role="$API_ROLE" -v worker_role="$WORKER_ROLE" \
  -v mcp_role="$MCP_ROLE" -v reporting_role="$REPORTING_ROLE" \
  -f /grant-matrix.sql

echo "grant-service-privileges: per-service privileges applied (#278)"
