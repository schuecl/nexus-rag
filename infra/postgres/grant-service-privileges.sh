#!/bin/bash
# Issue #317: the per-service GRANT matrix (#278), split out of
# apply-service-grants.sh so it has exactly one source of truth but two call
# sites:
#
#   1. The grant-service-privileges one-shot in docker-compose.yml, which runs
#      this alone, connected as the bootstrap superuser, right after
#      migrate-db-schema creates the tables and before ingestion-api/
#      ingestion-worker start. On a brand-new volume, migrate-db-schema's
#      init_db() now creates every table owned by the bootstrap superuser
#      (#314/#316) -- so INGESTION_API_DB_USER holds no privileges on them at
#      all until something grants some, and ingestion-api's lifespan
#      (_seed_defaults, main.py) needs SELECT+INSERT on classification_levels
#      before it can report healthy. The one-shot that used to grant that
#      (lock-down-db-grants/apply-service-grants.sh) only runs *after*
#      ingestion-api is healthy -- a cycle neither side can break on its own.
#      Running the matrix here first closes that window.
#   2. apply-service-grants.sh itself, which calls this again after its own
#      ownership-reassignment + REVOKE ALL, so lock-down-db-grants remains the
#      authority that also strips anything this early pass didn't need to
#      remove (nothing, on a fresh volume; a stale grant from a prior release,
#      on an upgrade).
#
# Granting does not require table ownership -- POSTGRES_USER is the bootstrap
# superuser, which can GRANT on any table regardless of who owns it -- so this
# is safe to run before ownership has ever been reassigned.
#
# The grant matrix itself is derived from what the code actually does, not
# from what seemed reasonable -- see apply-service-grants.sh's header comment
# for the full table and the reasoning behind what's deliberately withheld
# (no application role gets SELECT on audit_log, no DELETE on documents or the
# vocabulary tables).
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

PSQL="psql -v ON_ERROR_STOP=1 --username $POSTGRES_USER --dbname $POSTGRES_DB"

$PSQL <<-EOSQL
	-- ingestion-api: the upload and curation UI, the admin vocabulary editor,
	-- and the OIDC browser-login session store.
	GRANT SELECT, INSERT, UPDATE ON documents             TO "${API_ROLE}";
	GRANT SELECT, INSERT, UPDATE ON classification_levels TO "${API_ROLE}";
	GRANT SELECT, INSERT, UPDATE ON releasability_values  TO "${API_ROLE}";
	GRANT SELECT, INSERT, UPDATE ON portal_settings       TO "${API_ROLE}";
	GRANT SELECT, INSERT, UPDATE ON notifications         TO "${API_ROLE}";
	GRANT SELECT, INSERT, DELETE ON oauth_states          TO "${API_ROLE}";
	GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO "${API_ROLE}";
	GRANT INSERT                 ON audit_log             TO "${API_ROLE}";

	-- ingestion-worker: reads the queued row, writes back status and chunk
	-- counts. No session or OAuth tables at all -- it never sees a browser.
	GRANT SELECT, UPDATE         ON documents             TO "${WORKER_ROLE}";
	GRANT SELECT                 ON classification_levels TO "${WORKER_ROLE}";
	GRANT SELECT                 ON releasability_values  TO "${WORKER_ROLE}";
	GRANT INSERT                 ON audit_log             TO "${WORKER_ROLE}";

	-- orchestration-mcp: the retrieval path. It reads the classification
	-- ladder to build the FR-26 access filter and writes an audit row per
	-- query. That is the entire extent of its database access -- notably it
	-- cannot read documents at all, because chunk payloads come from Qdrant.
	GRANT SELECT                 ON classification_levels TO "${MCP_ROLE}";
	GRANT INSERT                 ON audit_log             TO "${MCP_ROLE}";

	-- audit-reporting (issue #309): the sole SELECT grantee on audit_log,
	-- and nothing else -- an offline reader that mines curator-decision
	-- audit entries to measure suggester-vs-curator agreement
	-- (scripts/calibrate_tagging_advisory.py). classification_levels is
	-- also readable so the script can rank-compare a flagged/suggested
	-- classification against the curator's final one; that vocabulary
	-- carries no access-control-sensitive data, unlike documents/audit_log.
	GRANT SELECT                 ON classification_levels TO "${REPORTING_ROLE}";
	GRANT SELECT                 ON audit_log             TO "${REPORTING_ROLE}";
EOSQL

# Sequences. audit_log/documents use application-generated ids, but any table
# with a SERIAL/IDENTITY column needs USAGE on its sequence for INSERT to work.
# Granted per role from the live catalogue rather than a fixed list, same
# reasoning as apply-service-grants.sh's ownership loop.
$PSQL <<-EOSQL
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "${API_ROLE}";
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "${WORKER_ROLE}";
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "${MCP_ROLE}";
EOSQL

echo "grant-service-privileges: per-service privileges applied (#278)"
