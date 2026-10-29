CREATE TABLE IF NOT EXISTS embedding_jobs (
 tenant_id text NOT NULL, kb_id text NOT NULL, build_id text NOT NULL,
 batch_no integer NOT NULL, dependency_hash text NOT NULL, provider_hash text NOT NULL,
 payload jsonb NOT NULL, status text NOT NULL DEFAULT 'PENDING'
 CHECK (status IN ('PENDING','RUNNING','COMPLETE','RETRY','ERROR','UNKNOWN')),
 attempt integer NOT NULL DEFAULT 0, request_id text, lease_until timestamptz,
 retry_at timestamptz, vectors jsonb, vectors_hash text, error text,
 PRIMARY KEY (tenant_id,kb_id,build_id,batch_no)
);
CREATE TABLE IF NOT EXISTS embedding_attempts (
 tenant_id text NOT NULL, kb_id text NOT NULL, build_id text NOT NULL,
 batch_no integer NOT NULL, request_id text NOT NULL,
 status text NOT NULL CHECK (status IN ('RUNNING','COMPLETE','ERROR','UNKNOWN')),
 diagnostic text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (tenant_id,kb_id,request_id)
);
CREATE TABLE IF NOT EXISTS embedding_circuits (
 tenant_id text NOT NULL, kb_id text NOT NULL, provider_hash text NOT NULL,
 failures integer NOT NULL DEFAULT 0, open_until timestamptz,
 PRIMARY KEY (tenant_id,kb_id,provider_hash)
);
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['embedding_jobs','embedding_attempts','embedding_circuits'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', name);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', name);
  EXECUTE format('DROP POLICY IF EXISTS kb_scope ON %I', name);
  EXECUTE format('CREATE POLICY kb_scope ON %I USING (tenant_id = current_setting(''app.tenant_id'', true) AND kb_id = current_setting(''app.kb_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true) AND kb_id = current_setting(''app.kb_id'', true))', name);
  EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO kb_app', name);
 END LOOP;
END $$;
