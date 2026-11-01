from contextlib import contextmanager
from pathlib import Path
import re
from threading import Lock

import psycopg
from psycopg.types.json import Jsonb

from .database_contracts import Migration, SchemaPackage, SpaceKey
from .database_providers.postgres import PostgresProvider, pg_transaction
from .database_service import DatabaseService
from .knowledge_queries import JsonValue, KnowledgeQuery
from .session_identity import PlatformError


DEFAULT_SPACE = SpaceKey('evidence-kb', 'knowledge', 'content')


def knowledge_schema(owner):
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


class KnowledgeTransaction:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=None):
        if not isinstance(query, KnowledgeQuery):
            raise ValueError('UNREGISTERED_KNOWLEDGE_QUERY')
        values = tuple(Jsonb(value.obj) if isinstance(value, JsonValue) else value
                       for value in (params or ()))
        return self._connection.execute(query.value, values)


class PostgresRepository:
    def __init__(self, dsn=None, *, database=None, connect=None):
        if database is not None and database.info.engine != 'postgresql':
            raise ValueError('KNOWLEDGE_POSTGRES_REQUIRED')
        self.dsn, self.database, self._connect = dsn, database, connect
        self._owned_service = None
        self._database_lock = Lock()
        if database is not None:
            database.prepare(knowledge_schema(database.identity.space.owner_id))
            database.seal()

    def _database(self):
        with self._database_lock:
            if self.database is None:
                service = DatabaseService()
                service.register('knowledge', PostgresProvider(self.dsn, provision=False, schema='public'))
                try:
                    database = service.bind(service.grant('knowledge', DEFAULT_SPACE, 'knowledge-repository'))
                    database.seal()
                except BaseException:
                    service.close()
                    raise
                self.database, self._owned_service = database, service
            return self.database

    @contextmanager
    def transaction(self, actor, kb_id=None):
        if not actor.tenant_id or not actor.principal_id:
            raise PlatformError('ACCESS_DENIED', 'Missing trusted transaction identity', 403)
        try:
            if self._connect is not None:
                from psycopg.rows import dict_row

                with self._connect(self.dsn, row_factory=dict_row, connect_timeout=5) as connection:
                    connection.execute(
                        "SELECT set_config('app.tenant_id', %s, true), set_config('app.kb_id', %s, true)",
                        (actor.tenant_id, kb_id or ''))
                    yield connection
            else:
                with pg_transaction(self._database(), actor.tenant_id, kb_id) as connection:
                    yield KnowledgeTransaction(connection)
        except psycopg.OperationalError as error:
            raise PlatformError('AUTHORITY_UNAVAILABLE', 'Authorization service unavailable', 503) from error

    def close(self):
        if self._owned_service is not None:
            self._owned_service.close()
            self._owned_service = None
