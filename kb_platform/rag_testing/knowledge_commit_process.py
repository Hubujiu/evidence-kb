import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from sqlalchemy.engine import Connection

from kb_platform.knowledge_schema import knowledge_schema
from kb_platform.knowledge_sessions import KnowledgeSessions
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_store import SessionStore
from .knowledge_session_fixture import session_fixture


def worker(path):
    request = json.loads(Path(path).read_text('utf-8'))
    backend = SessionStore(request['root'], schema=knowledge_schema)
    store = KnowledgeSessions(backend)
    original = Connection.commit

    def crash(connection):
        if request['phase'] == 'after':
            original(connection)
        os._exit(73)

    with patch.object(Connection, 'commit', crash):
        store.finish(Actor('tenant', 'reader'), request['session'], request['token'], {},
            {'status': 'answered', 'answer': 'Synthetic crash answer'}, authority=request['authority'],
            assistant_event={'text': 'Synthetic crash answer'})
    raise AssertionError('CRASH_POINT_NOT_REACHED')


def verify_crash_boundary(root):
    check = unittest.TestCase()
    results = []
    actor = Actor('tenant', 'reader')
    for phase in ('before', 'after'):
        store, authority = session_fixture(root / phase, actor)
        try:
            session = store.create(actor, 'scope', {})['session_id']
            token = store.begin(actor, session, 'turn', 0, 'fingerprint')['token']
            request = root / (phase + '.json')
            request.write_text(json.dumps({'root': str(root / phase), 'phase': phase,
                'session': session, 'token': token, 'authority': authority}), encoding='utf-8')
            store.close()
            completed = subprocess.run([sys.executable, '-m', __name__, str(request)],
                capture_output=True, text=True, timeout=10, check=False)
            (root / (phase + '-stderr.log')).write_text(completed.stderr, encoding='utf-8')
            check.assertEqual(completed.returncode, 73, completed.stderr)
            store = KnowledgeSessions(SessionStore(root / phase, schema=knowledge_schema))
            durable = phase == 'after'
            check.assertEqual(store.load(actor, session)['revision'], int(durable))
            check.assertEqual([row['kind'] for row in store.events(actor, session)],
                ['assistant', 'turn_completed'] if durable else [])
            if durable:
                check.assertEqual(store.begin(actor, session, 'turn', 0, 'fingerprint')['cached']['revision'], 1)
            else:
                with check.assertRaises(PlatformError) as caught:
                    store.begin(actor, session, 'turn', 0, 'fingerprint')
                check.assertEqual(caught.exception.code, 'TURN_IN_PROGRESS')
            from kb_platform.knowledge_schema import TENANTS

            with store.database.transaction(scope={'tenant_id': actor.tenant_id}) as transaction:
                transaction.execute(TENANTS.update().where(TENANTS.c.tenant_id == actor.tenant_id).values(revision=2))
            with check.assertRaises(PlatformError) as caught:
                store.finish(actor, session, token, {}, {'status': 'answered'}, authority=authority)
            check.assertEqual(caught.exception.code, 'SCOPE_CHANGED')
            results.append({'crash': phase + '_physical_commit', 'exit_code': completed.returncode,
                'durable_revision': int(durable), 'authority_lock_released': True, 'new_remote_calls': 0})
        finally:
            store.close()
    return results


if __name__ == '__main__':
    worker(sys.argv[1])
