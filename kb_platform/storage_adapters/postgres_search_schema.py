from ..database_contracts import Migration, SchemaPackage


BUILD_FUNCTION = 'storage_search_build(text,text,jsonb,integer,text)'
SEAL_FUNCTION = 'storage_search_seal(text,text)'


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS __DATABASE_SCHEMA__._storage_search_generations (
 generation text PRIMARY KEY, channel text NOT NULL CHECK(channel IN ('vector','lexical')),
 record_count integer NOT NULL CHECK(record_count>0), dimension integer,
 records_sha256 text NOT NULL, ready boolean NOT NULL DEFAULT false, seal text
);
CREATE OR REPLACE FUNCTION __DATABASE_SCHEMA__.storage_search_build(
 p_channel text,p_generation text,p_records jsonb,p_dimension integer,p_sha text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,__DATABASE_SCHEMA__,public AS $$
DECLARE item jsonb; record jsonb; field text; total integer;
BEGIN
 IF p_channel NOT IN ('vector','lexical') OR p_generation !~ '^sg_[0-9a-f]{40}$'
    OR jsonb_typeof(p_records)<>'array' OR jsonb_array_length(p_records)<1
    OR jsonb_array_length(p_records)>10000 OR p_sha !~ '^[0-9a-f]{64}$'
    OR (p_channel='vector' AND p_dimension IS NOT NULL AND (p_dimension<1 OR p_dimension>16000))
    OR (p_channel='lexical' AND p_dimension IS NOT NULL) THEN
   RAISE EXCEPTION 'Invalid search generation';
 END IF;
 INSERT INTO __DATABASE_SCHEMA__._storage_search_generations
  (generation,channel,record_count,dimension,records_sha256)
  VALUES(p_generation,p_channel,jsonb_array_length(p_records),p_dimension,p_sha);
 IF p_channel='vector' THEN
   EXECUTE format('CREATE TABLE __DATABASE_SCHEMA__.%I '
     '(record_id text PRIMARY KEY,metadata jsonb NOT NULL,record jsonb NOT NULL,embedding public.vector)',p_generation);
 ELSE
   EXECUTE format('CREATE TABLE __DATABASE_SCHEMA__.%I '
     '(record_id text PRIMARY KEY,metadata jsonb NOT NULL,record jsonb NOT NULL,'
     'body text NOT NULL,exact text NOT NULL,heading text NOT NULL)',p_generation);
 END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(p_records) LOOP
   record := item->'record';
   IF jsonb_typeof(record)<>'object' OR jsonb_typeof(record->'metadata')<>'object'
      OR jsonb_typeof(record->'id')<>'string' OR length(record->>'id')<1 THEN
     RAISE EXCEPTION 'Invalid search record';
   END IF;
   IF p_channel='vector' THEN
     IF record ? 'vector' AND (p_dimension IS NULL OR jsonb_typeof(record->'vector')<>'array'
        OR jsonb_array_length(record->'vector')<>p_dimension) THEN
       RAISE EXCEPTION 'Invalid search vector';
     END IF;
     EXECUTE format('INSERT INTO __DATABASE_SCHEMA__.%I VALUES($1,$2,$3,$4::public.vector)',p_generation)
       USING record->>'id',record->'metadata',record,(record->'vector')::text;
   ELSE
     EXECUTE format('INSERT INTO __DATABASE_SCHEMA__.%I VALUES($1,$2,$3,$4,$5,$6)',p_generation)
       USING record->>'id',record->'metadata',record,item->>'body',item->>'exact',item->>'heading';
   END IF;
 END LOOP;
 IF p_channel='lexical' THEN
   FOREACH field IN ARRAY ARRAY['body','exact','heading'] LOOP
     EXECUTE format('CREATE INDEX %I ON __DATABASE_SCHEMA__.%I USING bm25 (%I) '
       'WITH (text_config=''simple'')',p_generation||'_'||field,p_generation,field);
   END LOOP;
 END IF;
 EXECUTE format('SELECT count(*) FROM __DATABASE_SCHEMA__.%I',p_generation) INTO total;
 IF total<>jsonb_array_length(p_records) THEN
   RAISE EXCEPTION 'Search generation count mismatch';
 END IF;
 EXECUTE format('GRANT SELECT ON __DATABASE_SCHEMA__.%I TO %I',p_generation,session_user);
 UPDATE __DATABASE_SCHEMA__._storage_search_generations SET ready=true WHERE generation=p_generation;
END $$;
REVOKE ALL ON FUNCTION __DATABASE_SCHEMA__.storage_search_build(text,text,jsonb,integer,text) FROM PUBLIC;
CREATE OR REPLACE FUNCTION __DATABASE_SCHEMA__.storage_search_seal(p_generation text,p_seal text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,__DATABASE_SCHEMA__ AS $$
BEGIN
 IF p_seal IS NULL OR length(p_seal)<1 OR length(p_seal)>1024 THEN
   RAISE EXCEPTION 'Invalid search seal';
 END IF;
 UPDATE __DATABASE_SCHEMA__._storage_search_generations SET seal=p_seal
  WHERE generation=p_generation AND ready AND (seal IS NULL OR seal=p_seal);
 IF NOT FOUND THEN
   RAISE EXCEPTION 'Missing or differently sealed search generation';
 END IF;
END $$;
REVOKE ALL ON FUNCTION __DATABASE_SCHEMA__.storage_search_seal(text,text) FROM PUBLIC;
'''


def postgres_search_schema(owner):
    return SchemaPackage(owner, (Migration(1, (_SCHEMA,)),),
                         runtime_readonly=('_storage_search_generations',),
                         runtime_readonly_prefixes=('sg_',),
                         runtime_functions=(BUILD_FUNCTION, SEAL_FUNCTION))
