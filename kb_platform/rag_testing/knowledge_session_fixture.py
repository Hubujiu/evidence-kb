from sqlalchemy import select
from types import SimpleNamespace

from kb_platform.knowledge_schema import TENANTS, KNOWLEDGE_BASES, MEMBERS, knowledge_schema
from kb_platform.knowledge_sessions import KnowledgeSessions
from kb_platform.session_store import SessionStore


def session_fixture(root, actor, scope='scope', *, manifest=None, revision=1):
    backend = SessionStore(root, schema=knowledge_schema)
    with backend.database.transaction(scope={'tenant_id': actor.tenant_id, 'kb_id': scope}) as tx:
        if tx.execute(select(TENANTS).where(TENANTS.c.tenant_id == actor.tenant_id)).fetchone() is None:
            tx.insert(TENANTS, {'tenant_id': actor.tenant_id, 'revision': revision})
        if tx.execute(select(KNOWLEDGE_BASES).where(KNOWLEDGE_BASES.c.tenant_id == actor.tenant_id,
                KNOWLEDGE_BASES.c.kb_id == scope)).fetchone() is None:
            tx.insert(KNOWLEDGE_BASES, {'tenant_id': actor.tenant_id, 'kb_id': scope, 'name': 'Synthetic fixture',
                'owner_id': actor.principal_id, 'revision': 0, 'manifest_id': manifest})
            tx.insert(MEMBERS, {'tenant_id': actor.tenant_id, 'kb_id': scope,
                'principal_id': actor.principal_id, 'capabilities': ['kb.read', 'kb.manage']})
    return KnowledgeSessions(backend), {'scope_id': scope, 'manifest_id': manifest, 'authority_revision': revision}


def platform_fixture(testcase, root):
    from pathlib import Path
    from kb_platform.session_identity import Actor

    store, _ = session_fixture(Path(root) / 'authority-fixture', Actor('tenant', 'owner'))
    testcase.addCleanup(store.close)
    return SimpleNamespace(repository=SimpleNamespace(database=store.database),
        embedding_provider=None, ocr_client=None)
