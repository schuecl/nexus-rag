-- #133: metadata-only view for the Grafana "Documents" dashboard, created after
-- the application schema exists (run by the provision-metrics-view one-shot once
-- ingestion-api is healthy -- the `documents` table is built by SQLModel at
-- runtime, so this cannot live in postgres's initdb scripts). Exposes governance
-- metadata only. file_type is the filename extension alone (pdf/txt/md/...),
-- derived without exposing the filename itself; grafana_ro cannot read the base
-- tables. grafana_ro is created by init-app-roles.sh at first boot.
CREATE OR REPLACE VIEW document_metrics AS
  SELECT classification, doc_type, status, owner_org, created_at, updated_at,
         COALESCE(lower(substring(filename from '\.([^.]+)$')), 'none') AS file_type
  FROM documents;
GRANT SELECT ON document_metrics TO grafana_ro;
