#!/bin/bash
# Issue #278 (gap G2 of docs/roles-and-permissions.md): give each service only
# the privileges it actually uses, and take away everything else.
#
# This supersedes the harden-audit-log one-shot, which did the same job for one
# table. Folding it in rather than running both keeps the ordering honest: the
# ownership reassignment and the grant matrix have to happen in one pass, or
# there is a window where a table is owned by a role that has just been
# stripped of the privileges it needs.
#
# Why it runs here and not in ensure-roles.sh: every statement below names a
# table, and the tables do not exist until SQLModel's create_all() has run
# (common/db.py's init_db(), from ingestion-api's lifespan). So this runs as a
# one-shot after ingestion-api reports healthy, connecting as the bootstrap
# superuser -- the same shape, and the same reason, as the harden-audit-log
# step it replaces.
#
# The grant matrix below is derived from what the code actually does, not from
# what seemed reasonable. Anything a service does not do is not granted:
#
#   table                  ingestion-api          worker          orchestration-mcp
#   ---------------------  ---------------------  --------------  -----------------
#   documents              SELECT,INSERT,UPDATE   SELECT,UPDATE   --
#   classification_levels  SELECT,INSERT,UPDATE   SELECT          SELECT
#   releasability_values   SELECT,INSERT,UPDATE   SELECT          --
#   portal_settings        SELECT,INSERT,UPDATE   --              --
#   notifications          SELECT,INSERT,UPDATE   --              --
#   oauth_states           SELECT,INSERT,DELETE   --              --
#   user_sessions          SELECT,INSERT,UPDATE,DELETE  --        --
#   audit_log              INSERT                 INSERT          INSERT
#
# Three things in that table are worth stating explicitly, because each one is
# a deliberate decision rather than an omission:
#
# 1. NOBODY GETS SELECT ON audit_log. Not even ingestion-api. `select(
#    AuditLogEntry` appears nowhere outside the test suite -- every one of the
#    17 references across the three services constructs a row to insert. So
#    "no route exposes the audit log" (roles-and-permissions.md §2) is now true
#    at the database layer too, not just the HTTP layer. Reading the trail
#    becomes a deliberate DBA or SIEM act (NFR-2's export path), which is the
#    same boundary rag-admin already enforces above. harden-audit-log used to
#    grant SELECT here; that grant had no caller and is gone.
#
# 2. No DELETE on documents, for anyone. common/purge.py tombstones the row
#    (scrubs every content-bearing field, keeps the id so prior audit entries
#    still resolve) rather than deleting it -- so DELETE would only ever be
#    available for something the design says not to do.
#
# 3. No DELETE on classification_levels or releasability_values. The admin
#    routes retire a value by setting active = false, never by removing the
#    row, because a document tagged with a since-removed level would otherwise
#    become unclassifiable.
#
# UPDATE on user_sessions is what _refresh_session needs; DELETE is what
# _drop_session and the expiry reaper need (#108).
set -e

: "${POSTGRES_USER:?}"
: "${POSTGRES_DB:?}"

API_ROLE="${INGESTION_API_DB_USER:-nexus_rag_ingestion_api}"
WORKER_ROLE="${INGESTION_WORKER_DB_USER:-nexus_rag_ingestion_worker}"
MCP_ROLE="${ORCHESTRATION_MCP_DB_USER:-nexus_rag_orchestration_mcp}"
LEGACY_ROLE="${APP_DB_USER:-nexus_rag_app}"

PSQL="psql -v ON_ERROR_STOP=1 --username $POSTGRES_USER --dbname $POSTGRES_DB"

# Ownership first.
#
# REVOKE alone is not enough and never was: an owner always retains the right
# to GRANT on its own objects, so a role that owns a table can hand itself back
# anything taken from it. Only losing ownership outright closes that, which is
# why harden-audit-log reassigned rather than revoked. The same reasoning
# applies to every table, not just audit_log -- on a fresh volume they are all
# owned by whichever role ran create_all(), and on an upgrade they are all
# owned by the old shared role.
#
# Done as a loop over the live catalogue rather than a fixed list so a table
# added by a later release cannot silently keep its creator as owner. That is
# the #221 failure mode: a step that only covers what existed when it was
# written.
$PSQL <<-EOSQL
	DO \$\$
	DECLARE t record;
	BEGIN
	  FOR t IN
	    SELECT tablename FROM pg_tables
	    WHERE schemaname = 'public'
	      AND tableowner <> '${POSTGRES_USER}'
	  LOOP
	    EXECUTE format('ALTER TABLE public.%I OWNER TO %I', t.tablename, '${POSTGRES_USER}');
	    RAISE NOTICE 'reassigned owner of %', t.tablename;
	  END LOOP;
	END \$\$;
	EOSQL

# Postcondition, not decoration.
#
# Everything below assumes no application role owns a table any more. If the
# loop above silently reassigned nothing -- wrong database, an owner the loop's
# WHERE did not match, a table created after it ran -- then the grants that
# follow would apply cleanly, the script would exit 0, and the deployment would
# look hardened while every role could still GRANT itself back whatever it
# wanted. That is the exact failure mode this whole change is about, so it is
# asserted rather than assumed.
#
# This caught a real ambiguity during development: without it there is no way
# to tell "reassigned 8 tables" from "matched 0 rows" after the fact.
$PSQL <<-EOSQL
	DO \$\$
	DECLARE stragglers text;
	BEGIN
	  SELECT string_agg(tablename || ' (owned by ' || tableowner || ')', ', ')
	    INTO stragglers
	    FROM pg_tables
	   WHERE schemaname = 'public' AND tableowner <> '${POSTGRES_USER}';
	  IF stragglers IS NOT NULL THEN
	    RAISE EXCEPTION 'ownership reassignment did not take: %. Refusing to apply grants -- an owner can GRANT itself anything, so the result would look hardened and not be.', stragglers;
	  END IF;
	  RAISE NOTICE 'ownership verified: every public table is owned by ${POSTGRES_USER}';
	END \$\$;
	EOSQL

# Now revoke everything from every application role, so this script is the only
# thing that decides what they hold. Re-running it after a release that removed
# a grant actually removes it, instead of leaving the old one behind.
#
# The legacy shared role is included: on an upgrade it arrives holding ALL
# PRIVILEGES on the whole database, which is the thing this issue exists to
# remove. It keeps CONNECT so that an operator who has not yet rotated their
# DATABASE_URL gets an authorization error naming the table, rather than a
# connection failure that looks like the database is down.
for role in "$API_ROLE" "$WORKER_ROLE" "$MCP_ROLE" "$LEGACY_ROLE"; do
  $PSQL <<-EOSQL
	REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "${role}";
	REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM "${role}";
	REVOKE ALL PRIVILEGES ON DATABASE "${POSTGRES_DB}" FROM "${role}";
	GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${role}";
	EOSQL
done

# CREATE on the schema goes back too. ensure-roles.sh grants it before
# ingestion-api starts so create_all() can add a table a later release
# introduced; this closes the window again now that startup is done. USAGE
# stays -- without it the role cannot see the schema at all.
$PSQL <<-EOSQL
	REVOKE CREATE ON SCHEMA public FROM "${API_ROLE}";
	REVOKE CREATE ON SCHEMA public FROM "${WORKER_ROLE}";
	REVOKE CREATE ON SCHEMA public FROM "${MCP_ROLE}";
	REVOKE CREATE ON SCHEMA public FROM "${LEGACY_ROLE}";
	GRANT USAGE ON SCHEMA public TO "${API_ROLE}";
	GRANT USAGE ON SCHEMA public TO "${WORKER_ROLE}";
	GRANT USAGE ON SCHEMA public TO "${MCP_ROLE}";
EOSQL

# The matrix.
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
EOSQL

# Sequences. audit_log/documents use application-generated ids, but any table
# with a SERIAL/IDENTITY column needs USAGE on its sequence for INSERT to work.
# Granted per role from the live catalogue rather than a fixed list, same
# reasoning as the ownership loop.
$PSQL <<-EOSQL
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "${API_ROLE}";
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "${WORKER_ROLE}";
	GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "${MCP_ROLE}";
EOSQL

echo "apply-service-grants: per-service privileges applied (#278); audit_log is INSERT-only for every application role"
