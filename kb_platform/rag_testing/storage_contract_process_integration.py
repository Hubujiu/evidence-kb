from pathlib import Path
import sys
import unittest

from kb_platform.application_storage import StorageClient, storage_methods
from kb_platform.database_contracts import DatabaseError
from kb_platform.plugin_contracts import ContractApplication, ContractRegistry, Method
from kb_platform.session_identity import Actor
from kb_platform.session_store import SessionStore
from kb_platform.storage_catalog import initialize_configuration

from .knowledge_persistence_contracts import exercise_knowledge_repository


def verify_storage_contract_process(root):
    root = Path(root) / 'contract-storage-process'
    root.mkdir()
    fixture = Path(__file__).resolve().parents[2] / 'examples/contract-storage/worker.py'
    configuration = initialize_configuration({'version': 2, 'application_id': 'process-contract-storage',
        'instances': {'records': {'provider': 'sqlite', 'config': {'root': str(root / 'records')}}},
        'services': {'sessions': {'instance': 'records', 'export': 'structured'},
                     'knowledge': {'instance': 'records', 'export': 'structured'}},
        'bindings': {'store': {'primary': {'service': 'sessions', 'owner': 'sessions', 'space': 'history'}},
                     'repository': {'primary': {'service': 'knowledge', 'owner': 'knowledge', 'space': 'records'}}}})
    registry = ContractRegistry({('application.run', 1): {'run': Method(
        {'type': 'object', 'additionalProperties': False}, {'type': 'object'})},
        ('storage.service', 1): storage_methods()})
    catalog = [
        {'implementation': 'session-app', 'version': '1', 'executor': 'local',
         'provides': {'application': {'id': 'application.run', 'major': 1}},
         'requires': {'storage': {'contract': {'id': 'storage.service', 'major': 1}}}},
        {'implementation': 'storage-fixture', 'version': '1', 'executor': 'process',
         'command': [sys.executable, '-B', str(fixture)],
         'provides': {'storage': {'id': 'storage.service', 'major': 1}}, 'requires': {}}]
    document = {'version': 1, 'plugins': {
        'entry': {'implementation': 'session-app', 'version': '1', 'config': {}},
        'persistence': {'implementation': 'storage-fixture', 'version': '1', 'config': configuration}},
        'bindings': {'entry': {'storage': {'plugin': 'persistence', 'export': 'storage'}}},
        'entrypoint': {'plugin': 'entry', 'export': 'application'}}

    class Application:
        def __init__(self, dependency):
            self.database = StorageClient(dependency, 'store.primary').relational()
            self.repository = StorageClient(dependency, 'repository.primary').relational()
            self.store = SessionStore(database=self.database)

        def run(self, request):
            actor = Actor('tenant', 'owner')
            session = self.store.create(actor, 'kb', {'count': 0})
            sid = session['session_id']
            lease = self.store.begin(actor, sid, 'request', 0, 'fingerprint')
            self.store.append(actor, sid, lease['token'], 'message', {'content': 'fixture'}, 'request')
            artifact = self.store.artifact(actor, sid, lease['token'], {'evidence': ['process-boundary']})
            large_body = {'evidence': 'bounded structured transfer ' * 180000}
            large_artifact = self.store.artifact(actor, sid, lease['token'], large_body)
            response = self.store.finish(actor, sid, lease['token'], {'count': 1}, {'status': 'complete'})
            replay = self.store.begin(actor, sid, 'request', 0, 'fingerprint')
            if self.store.read_artifact(actor, sid, large_artifact['artifact_id']) != large_body:
                raise RuntimeError('STORAGE_LARGE_ARTIFACT_PROCESS_INTEGRITY_FAILED')
            with self.database.guard('protocol-check'):
                try:
                    with self.database.guard('protocol-check'):
                        raise RuntimeError('STORAGE_PROCESS_GUARD_NOT_EXCLUSIVE')
                except DatabaseError as error:
                    if error.code != 'DATABASE_GUARD_BUSY':
                        raise
            return {'state': self.store.load(actor, sid)['state'],
                    'artifact': self.store.read_artifact(actor, sid, artifact['artifact_id']),
                    'replayed': replay == {'cached': response},
                    'large_artifact': 'verified',
                    'events': [item['kind'] for item in self.store.events(actor, sid)],
                    'guard': 'exclusive', 'external_model_calls': 0,
                    'knowledge': exercise_knowledge_repository(unittest.TestCase(), self.repository, root / 'knowledge')}

    def mount(context, config):
        context.provide('application', Application(context.require('storage')))

    application = ContractApplication(document, catalog, registry=registry,
        mounts={('session-app', '1'): mount}, process_root=root,
        schemas={('session-app', '1'): {'type': 'object', 'additionalProperties': False},
                 ('storage-fixture', '1'): {'type': 'object'}})
    transport = application.host.service(application.plan['exports']['persistence']['storage'])._implementation.transport
    try:
        result = application.run({})
        if (result != {'state': {'count': 1}, 'artifact': {'evidence': ['process-boundary']},
                'replayed': True, 'events': ['message', 'turn_completed'], 'guard': 'exclusive', 'external_model_calls': 0,
                'large_artifact': 'verified',
                'knowledge': {'knowledge_build': 'VALIDATED', 'cross_tenant': 'DENIED', 'revoked_citation': 'DENIED',
                              'expired_job': 'UNKNOWN', 'external_model_calls': 0}}
                or application.host.inspect()['leases']):
            raise RuntimeError('STORAGE_CONTRACT_PROCESS_INTEGRATION_FAILED')
    finally:
        application.close()
    if (transport._process.poll() is None or transport._reader.is_alive() or transport._writer.is_alive()
            or application.host.inspect()['state'] != 'disposed'):
        raise RuntimeError('STORAGE_CONTRACT_PROCESS_CLEANUP_FAILED')
    return {**result, 'storage_runtime': transport.runtime_info['language'], 'host_state': 'disposed'}
