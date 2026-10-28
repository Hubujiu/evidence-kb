DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kb_app') THEN
    CREATE ROLE kb_app LOGIN PASSWORD 'kb_local_app' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS tenant_state (
  tenant_id text PRIMARY KEY,
  revision bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS knowledge_bases (
  tenant_id text NOT NULL,
  kb_id text NOT NULL,
  name text NOT NULL,
  owner_id text NOT NULL,
  revision bigint NOT NULL DEFAULT 0,
  manifest_id text,
  PRIMARY KEY (tenant_id, kb_id)
);
CREATE TABLE IF NOT EXISTS members (
  tenant_id text NOT NULL,
  kb_id text NOT NULL,
  principal_id text NOT NULL,
  capabilities jsonb NOT NULL,
  PRIMARY KEY (tenant_id, kb_id, principal_id),
  FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases (tenant_id, kb_id)
);
CREATE TABLE IF NOT EXISTS sources (
  tenant_id text NOT NULL,
  source_id text NOT NULL,
  owner_id text NOT NULL,
  readers jsonb NOT NULL,
  policy jsonb NOT NULL,
  revision bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, source_id)
);
CREATE TABLE IF NOT EXISTS source_versions (
  tenant_id text NOT NULL,
  source_id text NOT NULL,
  source_version_id text NOT NULL,
  filename text NOT NULL,
  original_path text NOT NULL,
  original_sha256 text NOT NULL,
  media_type text NOT NULL,
  PRIMARY KEY (tenant_id, source_id, source_version_id),
  FOREIGN KEY (tenant_id, source_id) REFERENCES sources (tenant_id, source_id)
);
CREATE TABLE IF NOT EXISTS parse_snapshots (
  tenant_id text NOT NULL,
  source_id text NOT NULL,
  source_version_id text NOT NULL,
  parse_snapshot_id text NOT NULL,
  document jsonb NOT NULL,
  PRIMARY KEY (tenant_id, source_id, source_version_id, parse_snapshot_id),
  FOREIGN KEY (tenant_id, source_id, source_version_id)
    REFERENCES source_versions (tenant_id, source_id, source_version_id)
);
CREATE TABLE IF NOT EXISTS bindings (
  tenant_id text NOT NULL,
  kb_id text NOT NULL,
  binding_id text NOT NULL,
  source_id text NOT NULL,
  source_version_id text NOT NULL,
  parse_snapshot_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('ACTIVE', 'REMOVED', 'SUSPENDED')),
  revision bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, kb_id, binding_id),
  UNIQUE (tenant_id, kb_id, source_id),
  FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases (tenant_id, kb_id),
  FOREIGN KEY (tenant_id, source_id, source_version_id, parse_snapshot_id)
    REFERENCES parse_snapshots (tenant_id, source_id, source_version_id, parse_snapshot_id)
);
CREATE TABLE IF NOT EXISTS builds (
  tenant_id text NOT NULL,
  kb_id text NOT NULL,
  build_id text NOT NULL,
  expected_manifest_id text,
  authority_revision bigint NOT NULL,
  kb_revision bigint NOT NULL,
  index_name text NOT NULL,
  inputs jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('STAGING', 'VALIDATED', 'PUBLISHED', 'FAILED')),
  unit_count integer NOT NULL,
  PRIMARY KEY (tenant_id, kb_id, build_id),
  FOREIGN KEY (tenant_id, kb_id) REFERENCES knowledge_bases (tenant_id, kb_id)
);
CREATE TABLE IF NOT EXISTS citations (
  tenant_id text NOT NULL,
  kb_id text NOT NULL,
  citation_id text NOT NULL,
  build_id text NOT NULL,
  binding_id text NOT NULL,
  source_id text NOT NULL,
  source_version_id text NOT NULL,
  parse_snapshot_id text NOT NULL,
  element_id text NOT NULL,
  text text NOT NULL,
  evidence_ids jsonb NOT NULL,
  anchors jsonb NOT NULL,
  table_data jsonb,
  PRIMARY KEY (tenant_id, kb_id, citation_id),
  FOREIGN KEY (tenant_id, kb_id, build_id) REFERENCES builds (tenant_id, kb_id, build_id),
  FOREIGN KEY (tenant_id, kb_id, binding_id) REFERENCES bindings (tenant_id, kb_id, binding_id),
  FOREIGN KEY (tenant_id, source_id, source_version_id, parse_snapshot_id)
    REFERENCES parse_snapshots (tenant_id, source_id, source_version_id, parse_snapshot_id)
);

DO $$ DECLARE name text; BEGIN
  FOREACH name IN ARRAY ARRAY['tenant_state','knowledge_bases','members','sources','source_versions','parse_snapshots'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', name);
    EXECUTE format('DROP POLICY IF EXISTS tenant_scope ON %I', name);
    EXECUTE format('CREATE POLICY tenant_scope ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', name);
  END LOOP;
  FOREACH name IN ARRAY ARRAY['bindings','builds','citations'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', name);
    EXECUTE format('DROP POLICY IF EXISTS kb_scope ON %I', name);
    EXECUTE format('CREATE POLICY kb_scope ON %I USING (tenant_id = current_setting(''app.tenant_id'', true) AND kb_id = current_setting(''app.kb_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true) AND kb_id = current_setting(''app.kb_id'', true))', name);
  END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO kb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO kb_app;
REVOKE UPDATE, DELETE ON source_versions, parse_snapshots, citations FROM kb_app;
