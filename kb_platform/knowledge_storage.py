from contextlib import contextmanager

from sqlalchemy import literal, select

from .database_contracts import DatabaseError, QueryLimits
from .knowledge_schema import knowledge_schema
from .knowledge_sql import execute_operation, operation_parameters
from .session_identity import Actor, PlatformError


KNOWLEDGE_TRANSACTION_LIMITS = QueryLimits(max_statements=20000)


class KnowledgeTransaction:
    def __init__(self, transaction, actor, kb_id, write):
        self._transaction = transaction
        self._actor, self._kb_id, self._write = actor, kb_id, write

    def execute(self, query, params=None):
        values = operation_parameters(query, params)
        trusted = {'tenant_id': self._actor.tenant_id, 'kb_id': self._kb_id,
                   'actor_principal_id': self._actor.principal_id}
        for key, value in trusted.items():
            if key in values and (value is None or values[key] != value):
                raise PlatformError('ACCESS_DENIED', 'Transaction scope cannot be replaced', 403)
        return execute_operation(self._transaction, query, values, write=self._write)


class KnowledgeRepository:
    def __init__(self, *, database):
        self.database, self.identity = database, database.identity
        database.prepare(knowledge_schema(database.identity.space.owner_id))
        database.seal()

    @contextmanager
    def transaction(self, actor, kb_id=None, *, write=True):
        if not isinstance(actor, Actor) or not actor.tenant_id or not actor.principal_id:
            raise PlatformError('ACCESS_DENIED', 'Missing trusted transaction identity', 403)
        try:
            with self.database.transaction(write=write, limits=KNOWLEDGE_TRANSACTION_LIMITS,
                    scope={'tenant_id': actor.tenant_id, 'kb_id': kb_id}) as transaction:
                yield KnowledgeTransaction(transaction, actor, kb_id, write)
        except DatabaseError as error:
            if error.code != 'DATABASE_BACKEND_UNAVAILABLE':
                raise
            raise PlatformError('AUTHORITY_UNAVAILABLE', 'Authorization service unavailable', 503) from error

    def job_transaction(self, tenant_id, kb_id, *, write=True):
        return self.transaction(Actor(tenant_id, 'embedding-worker'), kb_id, write=write)

    def probe(self):
        with self.database.transaction(write=False) as transaction:
            if transaction.execute(select(literal(1))).scalar() != 1:
                raise ValueError('KNOWLEDGE_STORAGE_UNAVAILABLE')
        return {'ready': True}
