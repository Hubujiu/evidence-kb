from contextlib import contextmanager

from sqlalchemy.exc import OperationalError

from .database_contracts import QueryLimits, SpaceKey, SqlStatement
from .knowledge_schema import knowledge_schema as knowledge_schema
from .knowledge_sql import execute_operation, operation_parameters
from .session_identity import Actor, PlatformError


DEFAULT_SPACE = SpaceKey('evidence-kb', 'knowledge', 'content')
KNOWLEDGE_TRANSACTION_LIMITS = QueryLimits(max_statements=20000)


class KnowledgeTransaction:
    def __init__(self, transaction, engine, actor, kb_id, write):
        self._transaction, self._engine = transaction, engine
        self._actor, self._kb_id, self._write = actor, kb_id, write

    def execute(self, query, params=None):
        values = operation_parameters(query, params)
        trusted = {'tenant_id': self._actor.tenant_id, 'kb_id': self._kb_id,
                   'actor_principal_id': self._actor.principal_id}
        for key, value in trusted.items():
            if key in values and (value is None or values[key] != value):
                raise PlatformError('ACCESS_DENIED', 'Transaction scope cannot be replaced', 403)
        return execute_operation(self._transaction, self._engine, query, values, write=self._write)


class KnowledgeRepository:
    def __init__(self, *, database, prepare=True):
        if database.info.engine not in {'postgresql', 'sqlite'}:
            raise ValueError('KNOWLEDGE_SQL_DIALECT_UNAVAILABLE')
        self.database, self.identity = database, database.identity
        if prepare:
            database.prepare(knowledge_schema(database.identity.space.owner_id, database.info.engine))
            database.seal()

    @contextmanager
    def transaction(self, actor, kb_id=None, *, write=True):
        if not isinstance(actor, Actor) or not actor.tenant_id or not actor.principal_id:
            raise PlatformError('ACCESS_DENIED', 'Missing trusted transaction identity', 403)
        try:
            with self.database.transaction(write=write, limits=KNOWLEDGE_TRANSACTION_LIMITS) as transaction:
                if self.database.info.engine == 'postgresql':
                    transaction.execute(SqlStatement(
                        "SELECT set_config('app.tenant_id', :tenant, true), "
                        "set_config('app.kb_id', :kb, true)",
                        {'tenant': actor.tenant_id, 'kb': kb_id or ''}))
                yield KnowledgeTransaction(transaction, self.database.info.engine, actor, kb_id, write)
        except OperationalError as error:
            raise PlatformError('AUTHORITY_UNAVAILABLE', 'Authorization service unavailable', 503) from error

    def job_transaction(self, tenant_id, kb_id, *, write=True):
        return self.transaction(Actor(tenant_id, 'embedding-worker'), kb_id, write=write)

    def probe(self):
        with self.database.transaction(write=False) as transaction:
            if transaction.execute(SqlStatement('SELECT 1')).scalar() != 1:
                raise ValueError('KNOWLEDGE_STORAGE_UNAVAILABLE')
        return {'ready': True, 'backend': self.database.info.engine}

    def close(self):
        pass
