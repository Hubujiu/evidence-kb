CREATE TABLE IF NOT EXISTS _database_identity (
 identity_key integer PRIMARY KEY, space_key varchar(1600) NOT NULL, dataset_id varchar(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS _database_migrations (
 schema_owner varchar(200) NOT NULL, version integer NOT NULL,
 content_hash varchar(64) NOT NULL, dirty integer NOT NULL, step integer NOT NULL,
 PRIMARY KEY(schema_owner,version)
);
INSERT INTO _database_identity(identity_key,space_key,dataset_id)
VALUES(1,'["evidence-kb","knowledge","content"]',replace(gen_random_uuid()::text,'-',''))
ON CONFLICT DO NOTHING;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON _database_identity,_database_migrations FROM kb_app;
GRANT SELECT ON _database_identity,_database_migrations TO kb_app;
