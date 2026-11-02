from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.application_storage import ApplicationStorage, StorageClient, storage_methods
from kb_platform.database_contracts import DatabaseError
from kb_platform.plugin_contracts import ContractApplication, ContractRegistry, Method, ServiceEndpoint
from kb_platform.storage_catalog import initialize_configuration
from kb_platform.storage_json_wire import MAX_VALUE_BYTES
from kb_platform.storage_process import encode_value
from kb_platform.storage_sdk import StorageError


def configuration(root):
    return initialize_configuration({'version': 2, 'application_id': 'generic-storage-contracts',
        'instances': {'records': {'provider': 'sqlite', 'config': {'root': str(root / 'records')}},
                      'search': {'provider': 'embedded', 'config': {'root': str(root / 'search')}}},
        'services': {'source': {'instance': 'records', 'export': 'document'},
                     'lexical': {'instance': 'search', 'export': 'lexical'},
                     'vector': {'instance': 'search', 'export': 'vector'}},
        'bindings': {'search': {
            'source': {'service': 'source', 'owner': 'search', 'space': 'source'},
            'bm25': {'service': 'lexical', 'owner': 'search', 'space': 'lexical'},
            'dense': {'service': 'vector', 'owner': 'search', 'space': 'vector'}}}})


@contextmanager
def storage():
    with TemporaryDirectory() as directory:
        service = ApplicationStorage(configuration(Path(directory)))
        try:
            yield service
        finally:
            service.close()


class TracedDependency:
    def __init__(self, dependency):
        self.dependency, self.messages = dependency, []

    def __getattr__(self, method):
        def call(request):
            result = getattr(self.dependency, method)(request)
            sizes = tuple(len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                          for item in (request, result))
            self.messages.append((method, request.get('request', {}).get('op'), *sizes))
            return result
        return call


def exercise_large_values(check, dependency):
    lexical = StorageClient(dependency, 'search.bm25')
    source = StorageClient(dependency, 'search.source')
    records = [{'id': f'record-{index:04d}', 'text': 'large record text ' * 200,
                'metadata': {f'field-{key}': index + key for key in range(16)}}
               for index in range(1600)]
    with check.assertRaisesRegex(StorageError, 'STORAGE_PROCESS_LIMIT'):
        encode_value(records)
    lexical.stage('generic-large', records)
    check.assertEqual(lexical.read_records('generic-large'), records)
    check.assertEqual(lexical.verify('generic-large')['count'], len(records))
    expected = [source.put(f'document-{index}', {'text': 'stored payload ' * 60000}, expected_revision=0)
                for index in range(6)]
    check.assertEqual(source.get_many([item.id for item in expected] + ['missing']), expected + [None])
    operations = [operation for method, operation, _, _ in dependency.messages if method == 'invoke']
    check.assertGreater(operations.count('wire_write'), 1)
    check.assertGreater(operations.count('wire_read'), 1)
    check.assertLessEqual(max(max(request, response) for _, _, request, response in dependency.messages),
                         4 * 1024 * 1024)
    return {'stage': 'VERIFIED', 'read_records': 'VERIFIED', 'get_many': 'VERIFIED',
            'bounded_frames': True, 'external_model_calls': 0}


class GenericStorageContracts(unittest.TestCase):
    def test_large_index_and_document_batch_cross_bounded_json_frames(self):
        with storage() as service:
            dependency = TracedDependency(ServiceEndpoint(service, storage_methods()))
            exercise_large_values(self, dependency)
            self.assertTrue(service._generic)
            self.assertTrue(all(not endpoint._transfers for endpoint in service._generic.values()))

    def test_failed_upload_or_lost_dispatch_response_never_replays(self):
        records = [{'id': 'large', 'text': 'bounded transfer ' * 300000, 'metadata': {}}]
        for operation in ('wire_write', 'wire_dispatch'):
            with self.subTest(operation=operation), storage() as service:
                dependency = TracedDependency(ServiceEndpoint(service, storage_methods()))
                client = StorageClient(dependency, 'search.bm25')
                original, failures = service.invoke, []
                def fail(request):
                    selected = request['request']['op'] == operation
                    if selected:
                        failures.append(request['request']['op'])
                        if operation == 'wire_write':
                            raise StorageError('SYNTHETIC_DISCONNECT')
                    result = original(request)
                    if selected:
                        raise StorageError('SYNTHETIC_DISCONNECT')
                    return result
                with patch.object(service, '_invoke', wraps=service._invoke) as dispatched:
                    with patch.object(service, 'invoke', side_effect=fail):
                        with self.assertRaisesRegex(StorageError, 'SYNTHETIC_DISCONNECT'):
                            client.stage('interrupted', records)
                    self.assertEqual(dispatched.call_count, int(operation == 'wire_dispatch'))
                self.assertEqual(failures, [operation])
                self.assertEqual(service._generic['search.bm25']._transfers, {})
                if operation == 'wire_dispatch':
                    self.assertEqual(client.read_records('interrupted'), records)

    def test_generic_envelopes_remain_bounded_and_binding_scoped(self):
        with storage() as service:
            def invoke(binding, request):
                return service.invoke({'binding': binding, 'request': request})
            logical = {'version': 1, 'op': 'invoke', 'method': 'get',
                       'args': [encode_value('missing')], 'kwargs': {}}
            for value in ({**logical, 'op': 'execute'}, {**logical, 'version': True},
                          {**logical, 'binding': 'search.dense'}, {**logical, 'kwargs': []},
                          {**logical, 'args': [encode_value(None)] * 65}):
                with self.subTest(value=value), self.assertRaises((StorageError, DatabaseError)):
                    invoke('search.source', value)
            for method in ('_methods', 'prepare', 'transaction', 'guard'):
                with self.subTest(method=method), self.assertRaisesRegex(StorageError, 'METHOD_REJECTED'):
                    invoke('search.source', {**logical, 'method': method})
            begin = {'version': 1, 'op': 'wire_begin', 'bytes': 3,
                     'sha256': sha256(b'yes').hexdigest(), 'transaction': None}
            for invalid in ({**begin, 'bytes': MAX_VALUE_BYTES + 1}, {**begin, 'transaction': 'other'}):
                with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_INVALID'):
                    invoke('search.source', invalid)
            opened = invoke('search.source', begin)
            with self.assertRaisesRegex(DatabaseError, 'STORAGE_WIRE_TRANSFER_CLOSED'):
                invoke('search.bm25', {'version': 1, 'op': 'wire_dispatch', 'handle': opened['handle']})
            endpoint = service._generic['search.source']
            stream = endpoint._transfers[opened['handle']].stream
            service.close()
            self.assertTrue(stream.closed)
            self.assertEqual(endpoint._transfers, {})
            with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_UNAVAILABLE'):
                invoke('search.source', logical)


def verify_generic_storage_process(root):
    root = Path(root) / 'generic-storage-process'
    root.mkdir()
    fixture = Path(__file__).resolve().parents[2] / 'examples/contract-storage/worker.py'
    registry = ContractRegistry({('application.run', 1): {'run': Method(
        {'type': 'object', 'additionalProperties': False}, {'type': 'object'})},
        ('storage.service', 1): storage_methods()})
    catalog = [
        {'implementation': 'generic-app', 'version': '1', 'executor': 'local',
         'provides': {'application': {'id': 'application.run', 'major': 1}},
         'requires': {'storage': {'contract': {'id': 'storage.service', 'major': 1}}}},
        {'implementation': 'storage-fixture', 'version': '1', 'executor': 'process',
         'command': [sys.executable, '-B', str(fixture)],
         'provides': {'storage': {'id': 'storage.service', 'major': 1}}, 'requires': {}}]
    document = {'version': 1, 'plugins': {
        'entry': {'implementation': 'generic-app', 'version': '1', 'config': {}},
        'persistence': {'implementation': 'storage-fixture', 'version': '1', 'config': configuration(root)}},
        'bindings': {'entry': {'storage': {'plugin': 'persistence', 'export': 'storage'}}},
        'entrypoint': {'plugin': 'entry', 'export': 'application'}}

    class Application:
        def __init__(self, dependency):
            self.dependency = TracedDependency(dependency)

        def run(self, request):
            return exercise_large_values(unittest.TestCase(), self.dependency)

    def mount(context, config):
        context.provide('application', Application(context.require('storage')))

    application = ContractApplication(document, catalog, registry=registry,
        mounts={('generic-app', '1'): mount}, process_root=root,
        schemas={('generic-app', '1'): {'type': 'object', 'additionalProperties': False},
                 ('storage-fixture', '1'): {'type': 'object'}})
    transport = application.host.service(application.plan['exports']['persistence']['storage'])._implementation.transport
    try:
        result = application.run({})
        if application.host.inspect()['leases']:
            raise RuntimeError('GENERIC_STORAGE_PROCESS_LEASE_LEAK')
    finally:
        application.close()
    if (transport._process.poll() is None or transport._reader.is_alive() or transport._writer.is_alive()
            or application.host.inspect()['state'] != 'disposed'):
        raise RuntimeError('GENERIC_STORAGE_PROCESS_CLEANUP_FAILED')
    return {**result, 'storage_runtime': transport.runtime_info['language'], 'host_state': 'disposed'}
