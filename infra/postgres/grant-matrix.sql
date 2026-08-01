-- Issues #278/#317/#319: the actual per-service GRANT matrix, as plain SQL so
-- it has exactly one source of truth for both call sites that need it:
--
--   1. grant-service-privileges.sh, run standalone before ingestion-api/
--      ingestion-worker start (#317) -- breaks the fresh-volume cycle where
--      ingestion-api can't report healthy without these grants, and
--      lock-down-db-grants (below) can't run until ingestion-api is healthy.
--   2. apply-service-grants.sh (the lock-down-db-grants one-shot), which
--      \i's this file from inside its own explicit BEGIN/COMMIT, immediately
--      after its REVOKE ALL pass, so REVOKE and re-GRANT commit as one
--      transaction (#319) -- no window where a role holds neither the old
--      nor the new privileges.
--
-- Both callers set the api_role/worker_role/mcp_role/reporting_role psql
-- variables (-v) before including this file; :"var" substitutes each as a
-- quoted identifier, not a literal.
--
-- Derived from what the code actually does, not from what seemed reasonable.
-- See apply-service-grants.sh's header comment for the full table and the
-- reasoning behind what's deliberately withheld (no application role gets
-- SELECT on audit_log, no DELETE on documents or the vocabulary tables).

-- ingestion-api: the upload and curation UI, the admin vocabulary editor,
-- and the OIDC browser-login session store.
GRANT SELECT, INSERT, UPDATE ON documents             TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON classification_levels TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON releasability_values  TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON portal_settings       TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON notifications         TO :"api_role";
GRANT SELECT, INSERT, DELETE ON oauth_states          TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions TO :"api_role";
GRANT INSERT                 ON audit_log             TO :"api_role";

-- ingestion-worker: reads the queued row, writes back status and chunk
-- counts. No session or OAuth tables at all -- it never sees a browser.
GRANT SELECT, UPDATE         ON documents             TO :"worker_role";
GRANT SELECT                 ON classification_levels TO :"worker_role";
GRANT SELECT                 ON releasability_values  TO :"worker_role";
GRANT INSERT                 ON audit_log             TO :"worker_role";

-- orchestration-mcp: the retrieval path. It reads the classification
-- ladder to build the FR-26 access filter and writes an audit row per
-- query. That is the entire extent of its database access -- notably it
-- cannot read documents at all, because chunk payloads come from Qdrant.
GRANT SELECT                 ON classification_levels TO :"mcp_role";
GRANT INSERT                 ON audit_log             TO :"mcp_role";

-- audit-reporting (issue #309): the sole SELECT grantee on audit_log,
-- and nothing else -- an offline reader that mines curator-decision
-- audit entries to measure suggester-vs-curator agreement
-- (scripts/calibrate_tagging_advisory.py). classification_levels is
-- also readable so the script can rank-compare a flagged/suggested
-- classification against the curator's final one; that vocabulary
-- carries no access-control-sensitive data, unlike documents/audit_log.
GRANT SELECT                 ON classification_levels TO :"reporting_role";
GRANT SELECT                 ON audit_log             TO :"reporting_role";

-- Sequences. audit_log/documents use application-generated ids, but any
-- table with a SERIAL/IDENTITY column needs USAGE on its sequence for
-- INSERT to work.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"api_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"worker_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"mcp_role";
