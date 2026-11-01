from pathlib import Path
import re

from .database_contracts import Migration, SchemaPackage


def postgres_schema(owner):
    directory = Path(__file__).resolve().parents[1] / 'sql'
    statements = []
    for filename in ('001_init.sql', '002_search.sql', '003_embedding_jobs.sql'):
        source = (directory / filename).read_text('utf-8')
        if filename == '001_init.sql':
            source = source.split('END $$;', 1)[1]
        source = re.sub(r'CREATE EXTENSION IF NOT EXISTS (vector|pg_textsearch);',
                        r'CREATE EXTENSION IF NOT EXISTS \1 WITH SCHEMA __EXTENSION_SCHEMA__;', source)
        source = source.replace('public.vector', '__EXTENSION_VECTOR__')
        source = re.sub(r'\bpublic\b', '__DATABASE_SCHEMA__', source)
        source = source.replace('kb_search', '__DATABASE_SCHEMA__')
        source = source.replace('kb_app', '__DATABASE_RUNTIME_ROLE__')
        source = source.replace('__EXTENSION_VECTOR__', 'public.vector')
        source = source.replace('__EXTENSION_SCHEMA__', 'public')
        statements.append(source)
    return SchemaPackage(owner, (Migration(1, tuple(statements)),),
        runtime_readonly=('search_generations',), runtime_readonly_prefixes=('g_',),
        runtime_appendonly=('source_versions', 'parse_snapshots', 'citations'),
        runtime_functions=('search_scope(text)', 'create_search_generation(text,integer,integer,text)',
                           'finish_search_generation(text)', 'seal_search_generation(text,text)'))


def sqlite_schema(owner):
    directory = Path(__file__).resolve().parents[1] / 'sql'
    content = (directory / '001_init.sql').read_text('utf-8').split('END $$;', 1)[1]
    content = content.split('DO $$ DECLARE name text;', 1)[0]
    jobs = (directory / '003_embedding_jobs.sql').read_text('utf-8').split('DO $$ DECLARE name text;', 1)[0]
    content = (content + jobs).replace('jsonb', 'TEXT').replace('timestamptz', 'TEXT')
    content = content.replace('DEFAULT now()', 'DEFAULT CURRENT_TIMESTAMP')
    statements = tuple(part.strip() for part in content.split(';') if part.strip())
    return SchemaPackage(owner, (Migration(1, statements),),
        runtime_appendonly=('source_versions', 'parse_snapshots', 'citations'))


def knowledge_schema(owner, engine='postgresql'):
    if engine == 'postgresql':
        return postgres_schema(owner)
    if engine == 'sqlite':
        return sqlite_schema(owner)
    raise ValueError('KNOWLEDGE_SQL_DIALECT_UNAVAILABLE')

