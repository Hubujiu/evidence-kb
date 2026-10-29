CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE SCHEMA IF NOT EXISTS kb_search;
REVOKE ALL ON SCHEMA kb_search FROM PUBLIC;
GRANT USAGE ON SCHEMA kb_search TO kb_app;
CREATE TABLE IF NOT EXISTS public.search_generations (
  index_name text PRIMARY KEY, tenant_id text NOT NULL, kb_id text NOT NULL,
  dimension integer, unit_count integer NOT NULL, units_sha256 text NOT NULL,
  ready boolean NOT NULL DEFAULT false, seal text
);
ALTER TABLE public.search_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_generations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS search_scope ON public.search_generations;
CREATE POLICY search_scope ON public.search_generations USING
 (tenant_id=current_setting('app.tenant_id',true) AND kb_id=current_setting('app.kb_id',true));
GRANT SELECT ON public.search_generations TO kb_app;

CREATE OR REPLACE FUNCTION public.search_scope(p_index text)
RETURNS TABLE(tenant_id text,kb_id text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
 SELECT tenant_id,kb_id FROM public.search_generations WHERE index_name=p_index;
$$;
REVOKE ALL ON FUNCTION public.search_scope(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.search_scope(text) TO kb_app;

CREATE OR REPLACE FUNCTION public.create_search_generation(p_index text, p_dimension integer, p_count integer, p_sha text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,public AS $$
DECLARE suffix text; tab text; tenant text := current_setting('app.tenant_id'); kb text := current_setting('app.kb_id');
BEGIN
 IF tenant='' OR kb='' OR p_index !~ '^kb-[0-9a-f]{24}-[0-9a-f]{32}$' OR p_count<1
    OR (p_dimension IS NOT NULL AND (p_dimension<1 OR p_dimension>16000)) THEN
   RAISE EXCEPTION 'Invalid search generation';
 END IF;
 INSERT INTO public.search_generations VALUES (p_index,tenant,kb,p_dimension,p_count,p_sha,false,NULL);
 FOREACH suffix IN ARRAY ARRAY['b','p'] LOOP
   tab := 'g_' || md5(p_index) || '_' || suffix;
   EXECUTE format('CREATE TABLE kb_search.%I (rowid bigint PRIMARY KEY, unit_id text UNIQUE NOT NULL,
     tenant_id text NOT NULL CHECK(tenant_id=%L), kb_id text NOT NULL CHECK(kb_id=%L),
     binding_key text NOT NULL, kind text NOT NULL, citation_id text NOT NULL,
     data jsonb NOT NULL, embedding public.vector, body text NOT NULL, exact text NOT NULL, heading text NOT NULL)',tab,tenant,kb);
   EXECUTE format('ALTER TABLE kb_search.%I ENABLE ROW LEVEL SECURITY',tab);
   EXECUTE format('ALTER TABLE kb_search.%I FORCE ROW LEVEL SECURITY',tab);
   EXECUTE format('CREATE POLICY scope ON kb_search.%I USING (tenant_id=current_setting(''app.tenant_id'',true) AND kb_id=current_setting(''app.kb_id'',true))',tab);
   EXECUTE format('GRANT SELECT,INSERT ON kb_search.%I TO kb_app',tab);
 END LOOP;
END $$;
REVOKE ALL ON FUNCTION public.create_search_generation(text,integer,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.create_search_generation(text,integer,integer,text) TO kb_app;

CREATE OR REPLACE FUNCTION public.finish_search_generation(p_index text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE suffix text; tab text; field text; generation public.search_generations; total bigint:=0; n bigint;
BEGIN
 SELECT * INTO STRICT generation FROM public.search_generations WHERE index_name=p_index
   AND tenant_id=current_setting('app.tenant_id') AND kb_id=current_setting('app.kb_id') FOR UPDATE;
 IF generation.ready THEN RAISE EXCEPTION 'Generation is immutable'; END IF;
 FOREACH suffix IN ARRAY ARRAY['b','p'] LOOP
   tab := 'g_' || md5(p_index) || '_' || suffix;
   EXECUTE format('SELECT count(*) FROM kb_search.%I',tab) INTO n;
   total := total+n;
   FOREACH field IN ARRAY ARRAY['body','exact','heading'] LOOP
     EXECUTE format('CREATE INDEX %I ON kb_search.%I USING bm25 (%I) WITH (text_config=''simple'')',tab||'_'||field,tab,field);
   END LOOP;
   EXECUTE format('CREATE INDEX ON kb_search.%I (binding_key)',tab);
   EXECUTE format('REVOKE INSERT ON kb_search.%I FROM kb_app',tab);
   EXECUTE format('ANALYZE kb_search.%I',tab);
 END LOOP;
 IF total<>generation.unit_count THEN RAISE EXCEPTION 'Generation count mismatch'; END IF;
 UPDATE public.search_generations SET ready=true WHERE index_name=p_index;
END $$;
REVOKE ALL ON FUNCTION public.finish_search_generation(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.finish_search_generation(text) TO kb_app;

CREATE OR REPLACE FUNCTION public.seal_search_generation(p_index text,p_seal text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 UPDATE public.search_generations SET seal=p_seal WHERE index_name=p_index AND ready
  AND tenant_id=current_setting('app.tenant_id') AND kb_id=current_setting('app.kb_id')
  AND (seal IS NULL OR seal=p_seal);
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing or differently sealed generation'; END IF;
END $$;
REVOKE ALL ON FUNCTION public.seal_search_generation(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.seal_search_generation(text,text) TO kb_app;

CREATE TABLE IF NOT EXISTS public.search_manifests (
 tenant_id text NOT NULL, kb_id text NOT NULL, manifest jsonb NOT NULL, checksum text NOT NULL,
 PRIMARY KEY(tenant_id,kb_id)
);
ALTER TABLE public.search_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_manifests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS manifest_scope ON public.search_manifests;
CREATE POLICY manifest_scope ON public.search_manifests USING
 (tenant_id=current_setting('app.tenant_id',true) AND kb_id=current_setting('app.kb_id',true));
GRANT SELECT,INSERT,UPDATE ON public.search_manifests TO kb_app;
