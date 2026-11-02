import base64
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.database_contracts import DatabaseError
from kb_platform.knowledge_platform_contract import CHUNK_BYTES, METHODS, PlatformClient, PlatformEndpoint
from kb_platform.model_errors import RemoteModelError
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_native import PluginError
from kb_platform.session_identity import Actor, PlatformError


class RecordingDependency:
    def __init__(self, endpoint):
        self.endpoint, self.requests, self.responses = endpoint, [], []

    def __getattr__(self, name):
        operation = getattr(self.endpoint, name)

        def call(request):
            self.requests.append((name, json.loads(json.dumps(request))))
            response = operation(request)
            self.responses.append((name, json.loads(json.dumps(response))))
            return response
        return call


class KnowledgePlatformContracts(unittest.TestCase):
    def fixture(self):
        platform = SimpleNamespace(
            embedding_provider=SimpleNamespace(provider_id='embedding-a', manifest={'dimensions': 8}),
            ocr_client=SimpleNamespace(provider_id='ocr-a', manifest={'model': 'fixture'}),
            create_kb=Mock(return_value={'kb_id': 'kb-a', 'name': 'New knowledge base'}),
            search=Mock(return_value={'hits': [], 'context': ''}),
            resolve_citation=Mock(return_value={'citation_id': 'citation-a', 'original_path': '/private/file',
                                               'text': 'Visible evidence'}),
            ingest=Mock(return_value={'source_id': 'source-a'}),
            read_original=Mock(return_value=b'original'),
            authorize_ingest_result=Mock())
        original_identity = {'kb_id': 'kb-a', 'citation_id': 'citation-a', 'source_id': 'source-a',
            'source_version_id': 'version-a', 'parse_snapshot_id': 'parse-a', 'build_id': 'build-a',
            'processing_policy': {'ocr': True}}
        platform.authorize_original = Mock(side_effect=lambda *args, **kwargs: {
            **original_identity, 'original_sha256': sha256(platform.read_original.return_value).hexdigest()})
        endpoint = PlatformEndpoint(platform)
        dependency = RecordingDependency(ServiceEndpoint(endpoint, METHODS))
        client = PlatformClient(dependency)
        self.addCleanup(endpoint.close)
        return SimpleNamespace(platform=platform, endpoint=endpoint, dependency=dependency,
                               client=client, actor=Actor('tenant', 'owner'), original_identity=original_identity)

    def begin(self, case, data):
        return case.dependency.ingest_begin({'actor': {'tenant_id': 'tenant', 'principal_id': 'owner'},
            'kb_id': 'kb-a', 'filename': 'source.txt', 'readers': None, 'policy': None, 'source_id': None,
            'size': len(data), 'sha256': sha256(data).hexdigest()})

    def test_public_methods_roundtrip_actor_and_keep_internal_storage_private(self):
        case = self.fixture()
        result = case.client.create_kb(case.actor, 'New knowledge base')
        self.assertEqual(result['kb_id'], 'kb-a')
        case.platform.create_kb.assert_called_once_with(actor=case.actor, name='New knowledge base')
        case.client.search(case.actor, 'kb-a', 'query', channel='bm25', top_k=4)
        case.platform.search.assert_called_once_with(actor=case.actor, kb_id='kb-a', query='query',
                                                    channel='bm25', top_k=4)
        self.assertEqual(case.client.resolve_citation(case.actor, 'kb-a', 'citation-a'),
                         {'citation_id': 'citation-a', 'text': 'Visible evidence'})
        for private in ('_tx', 'repository', 'store', 'parser', 'close', 'execute'):
            self.assertFalse(hasattr(case.client, private), private)
            self.assertFalse(hasattr(case.dependency, private), private)

    def test_provider_capabilities_contain_json_metadata_without_provider_handles(self):
        case = self.fixture()
        self.assertEqual(case.client.embedding_provider.manifest, {'dimensions': 8})
        self.assertEqual(case.client.ocr_client.provider_id, 'ocr-a')
        self.assertFalse(hasattr(case.client.embedding_provider, 'embed'))
        self.assertFalse(hasattr(case.client.ocr_client, 'complete'))
        case.platform.embedding_provider = case.platform.ocr_client = None
        client = PlatformClient(case.dependency)
        self.assertIsNone(client.embedding_provider)
        self.assertIsNone(client.ocr_client)

    def test_binary_upload_and_download_above_transport_limit_use_bounded_chunks(self):
        case = self.fixture()
        data = bytes(range(256)) * 17000
        case.platform.read_original.return_value = data
        self.assertEqual(case.client.ingest(case.actor, 'kb-a', 'original.bin', data,
            ['reader'], {'preview': True}, 'existing-source'), {'source_id': 'source-a'})
        case.platform.ingest.assert_called_once_with(case.actor, kb_id='kb-a', filename='original.bin',
            data=data, readers=['reader'], policy={'preview': True}, source_id='existing-source')
        self.assertEqual(case.client.read_original(case.actor, 'kb-a', 'citation-a'), data)
        case.platform.read_original.assert_called_once()
        self.assertEqual(case.platform.authorize_original.call_count, 1 + (len(data) + CHUNK_BYTES - 1) // CHUNK_BYTES)
        for call in case.platform.authorize_original.call_args_list[1:]:
            self.assertEqual(call.kwargs['processing_policy'], {'ocr': True})
        transfers = [(name, request) for name, request in case.dependency.requests if name in {
            'ingest_append', 'read_original_chunk'}]
        self.assertGreater(len(transfers), 2)
        self.assertTrue(all(len(json.dumps(request).encode()) < 4 * 1024 * 1024 for _, request in transfers))
        self.assertTrue(all(len(json.dumps(response).encode()) < 4 * 1024 * 1024
                            for _, response in case.dependency.responses))
        self.assertEqual(case.endpoint._uploads, {})
        self.assertEqual(case.endpoint._transfers._handles, {})

    def test_upload_tokens_are_actor_bound_and_require_exact_order_and_content_hash(self):
        case = self.fixture()
        identity = self.begin(case, b'abc')['upload_id']
        owner = {'tenant_id': 'tenant', 'principal_id': 'owner'}
        request = {'actor': owner, 'upload_id': identity, 'offset': 0,
                   'data_base64': base64.b64encode(b'abd').decode()}
        with self.assertRaises(ContractError) as caught:
            case.dependency.ingest_append({**request, 'actor': {**owner, 'principal_id': 'other'}})
        self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFER_UNAVAILABLE')
        with self.assertRaises(ContractError) as caught:
            case.dependency.ingest_append({**request, 'offset': 1})
        self.assertEqual(caught.exception.code, 'INVALID_PLATFORM_TRANSFER')
        case.dependency.ingest_append(request)
        with self.assertRaises(ContractError) as caught:
            case.dependency.ingest_finish({'actor': owner, 'upload_id': identity})
        self.assertEqual(caught.exception.code, 'INVALID_PLATFORM_TRANSFER')
        case.platform.ingest.assert_not_called()
        self.assertTrue(case.dependency.ingest_cancel({'actor': owner, 'upload_id': identity})['cancelled'])

    def test_download_detects_source_change_between_chunks(self):
        case = self.fixture()
        case.platform.read_original.return_value = b'a' * (CHUNK_BYTES + 5)
        target = case.platform.authorize_original(None)
        case.platform.authorize_original.reset_mock()
        case.platform.authorize_original.side_effect = [target, target, {**target, 'source_version_id': 'changed'}]
        with self.assertRaises(PlatformError) as caught:
            case.client.read_original(case.actor, 'kb-a', 'citation-a')
        self.assertEqual(caught.exception.code, 'SOURCE_INTEGRITY')
        case.platform.read_original.assert_called_once()
        self.assertEqual(case.endpoint._transfers._handles, {})

    def test_download_rechecks_revocation_before_each_chunk_and_discards_snapshot(self):
        case = self.fixture()
        case.platform.read_original.return_value = b'a' * (CHUNK_BYTES + 5)
        target = case.platform.authorize_original(None)
        case.platform.authorize_original.reset_mock()
        case.platform.authorize_original.side_effect = [target, target, PlatformError('ACCESS_DENIED', 'Revoked', 403)]
        with self.assertRaises(PlatformError) as caught:
            case.client.read_original(case.actor, 'kb-a', 'citation-a')
        self.assertEqual((caught.exception.code, caught.exception.status_code), ('ACCESS_DENIED', 403))
        case.platform.read_original.assert_called_once()
        self.assertEqual(case.endpoint._transfers._handles, {})

    def download(self, case):
        actor = {'tenant_id': 'tenant', 'principal_id': 'owner'}
        ticket = case.dependency.read_original_begin({'actor': actor, 'kb_id': 'kb-a',
                                                     'citation_id': 'citation-a', 'purpose': 'download'})
        return {'actor': actor, 'transfer_id': ticket['transfer_id']}

    def test_download_ticket_binds_actor_kind_offset_and_hash(self):
        case = self.fixture()
        request = self.download(case)
        snapshot = case.endpoint._transfers._handles[request['transfer_id']]
        for changed in ({'actor': {'tenant_id': 'tenant', 'principal_id': 'other'}}, {'offset': 1}):
            with self.assertRaises(ContractError):
                case.dependency.read_original_chunk({**request, 'offset': 0, **changed})
        with self.assertRaises(ContractError):
            case.dependency.ingest_result_chunk({**request, 'offset': 0})
        self.assertFalse(case.dependency.transfer_cancel({**request,
            'actor': {'tenant_id': 'other-tenant', 'principal_id': 'owner'}})['cancelled'])
        self.assertTrue(case.dependency.transfer_cancel(request)['cancelled'])
        self.assertTrue(snapshot.stream.closed)
        self.assertEqual(case.endpoint._transfers._handles, {})
        operation = case.dependency.read_original_chunk

        def corrupt(request):
            response = operation(request)
            return {**response, 'data_base64': base64.b64encode(b'corrupt!').decode()}

        case.dependency.read_original_chunk = corrupt
        with self.assertRaises(PlatformError) as caught:
            case.client.read_original(case.actor, 'kb-a', 'citation-a')
        self.assertEqual(caught.exception.code, 'SOURCE_INTEGRITY')
        self.assertEqual(case.endpoint._transfers._handles, {})

    def test_download_expiry_capacity_and_close_release_all_snapshots(self):
        case = self.fixture()
        with patch('kb_platform.platform_transfers.monotonic', return_value=0):
            request = self.download(case)
        snapshot = case.endpoint._transfers._handles[request['transfer_id']]
        with patch('kb_platform.platform_transfers.monotonic', return_value=301):
            with self.assertRaises(ContractError) as caught:
                case.dependency.read_original_chunk({**request, 'offset': 0})
        self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFER_UNAVAILABLE')
        self.assertTrue(snapshot.stream.closed)
        self.download(case)
        calls = case.platform.read_original.call_count
        with patch('kb_platform.platform_transfers.MAX_TRANSFERS', 1):
            with self.assertRaises(ContractError) as caught:
                self.download(case)
        self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFERS_BUSY')
        self.assertEqual(case.platform.read_original.call_count, calls)
        streams = [snapshot.stream for snapshot in case.endpoint._transfers._handles.values()]
        case.endpoint.close()
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(case.endpoint._transfers._handles, {})

    def large_ingest(self, case):
        result = {'source_id': 'source-a', 'source_version_id': 'version-a',
            'document': {'parse_snapshot_id': 'parse-a', 'status': 'READY',
                         'elements': [{'raw_text': 'a' * (5 * 1024 * 1024)}]}}
        case.platform.ingest.return_value = result
        return result

    def test_large_ingest_result_preserves_document_without_repeating_the_write(self):
        case = self.fixture()
        expected = self.large_ingest(case)
        result = case.client.ingest(case.actor, 'kb-a', 'original.txt', b'content')
        self.assertEqual(result, expected)
        case.platform.ingest.assert_called_once()
        self.assertGreater(case.platform.authorize_ingest_result.call_count, 1)
        self.assertTrue(all(len(json.dumps(response).encode()) < 4 * 1024 * 1024
                            for _, response in case.dependency.responses))
        self.assertEqual(case.endpoint._transfers._handles, {})

    def test_large_ingest_result_revocation_and_failed_reads_do_not_repeat_committed_ingest(self):
        for error in (PlatformError('ACCESS_DENIED', 'Revoked', 403), OSError('transport lost')):
            with self.subTest(error=type(error).__name__):
                case = self.fixture()
                self.large_ingest(case)
                case.platform.authorize_ingest_result.side_effect = [None, None, error]
                with self.assertRaises(PlatformError):
                    case.client.ingest(case.actor, 'kb-a', 'original.txt', b'content')
                case.platform.ingest.assert_called_once()
                self.assertEqual(case.endpoint._transfers._handles, {})

    def test_ingest_reserves_result_capacity_before_dispatching_write(self):
        case = self.fixture()
        self.download(case)
        with patch('kb_platform.platform_transfers.MAX_TRANSFERS', 1):
            with self.assertRaises(PlatformError) as caught:
                case.client.ingest(case.actor, 'kb-a', 'original.txt', b'content')
        self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFERS_BUSY')
        case.platform.ingest.assert_not_called()
        self.assertEqual(case.endpoint._uploads, {})

    def test_real_authority_revocation_stops_snapshot_without_another_original_read(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from kb_platform.core import Platform
        from kb_platform.database_contracts import SpaceKey
        from kb_platform.documents import parse_native_document
        from kb_platform.embedded_search import EmbeddedSearch
        from kb_platform.knowledge_storage import KnowledgeRepository
        from kb_platform.persistence_local import local_space
        from kb_platform.storage import OriginalStore

        for revoke in ('reader', 'binding'):
            with self.subTest(revoke=revoke), TemporaryDirectory() as directory:
                root = Path(directory)
                database, owner = local_space(root / 'database', SpaceKey('snapshot-test', 'repository', 'primary'))
                backend = EmbeddedSearch(root / 'search')
                store = OriginalStore(root / 'originals')
                endpoint = None
                try:
                    platform = Platform(repository=KnowledgeRepository(database=database), search=backend,
                        store=store, parser=parse_native_document, embedding_jobs=None)
                    actor, reader = Actor('tenant', 'owner'), Actor('tenant', 'reader')
                    scope = platform.create_kb(actor, 'Snapshot authorization')['kb_id']
                    platform.set_member(actor, scope, 'reader', ['kb.read'])
                    source = platform.ingest(actor, scope, 'original.txt', b'Original evidence value 42.',
                                             readers=['reader'])
                    build = platform.stage(actor, scope, ['bm25'])
                    platform.publish(actor, scope, build['build_id'])
                    citation = platform.search(reader, scope, 'evidence')['hits'][0]['citation_id']
                    endpoint = PlatformEndpoint(platform)
                    dependency = RecordingDependency(ServiceEndpoint(endpoint, METHODS))
                    client = PlatformClient(dependency)
                    operation = dependency.read_original_chunk

                    def chunk(request):
                        result = operation(request)
                        if request['offset'] == 0:
                            if revoke == 'reader':
                                platform.set_source_readers(actor, source['source_id'], [])
                            else:
                                platform.remove_binding(actor, scope, source['binding_id'])
                        return result

                    dependency.read_original_chunk = chunk
                    with patch.object(store, 'read', wraps=store.read) as read:
                        with patch('kb_platform.platform_transfers.CHUNK_BYTES', 8):
                            with self.assertRaises(PlatformError) as caught:
                                client.read_original(reader, scope, citation)
                        read.assert_called_once()
                    self.assertEqual(caught.exception.status_code, 403)
                    self.assertEqual(endpoint._transfers._handles, {})
                finally:
                    if endpoint is not None:
                        endpoint.close()
                    backend.close()
                    owner.close()

    def test_domain_errors_and_provider_status_survive_the_boundary(self):
        case = self.fixture()
        for error, code, status in ((PlatformError('ACCESS_DENIED', 'Denied', 403), 'ACCESS_DENIED', 403),
                                    (DatabaseError('DATABASE_UNAVAILABLE'), 'AUTHORITY_UNAVAILABLE', 503)):
            case.platform.create_kb.side_effect = error
            with self.assertRaises(PlatformError) as caught:
                case.client.create_kb(case.actor, 'Name')
            self.assertEqual((caught.exception.code, caught.exception.status_code), (code, status))
        case.platform.search.side_effect = RemoteModelError('PROVIDER_RATE_LIMITED', 429, retry_after_seconds=5)
        with self.assertRaises(RemoteModelError) as caught:
            case.client.search(case.actor, 'kb-a', 'query')
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after_seconds, 5)

    def test_uploads_expire_and_close_releases_pending_content(self):
        case = self.fixture()
        with patch('kb_platform.knowledge_platform_contract.monotonic', return_value=0):
            identity = self.begin(case, b'abc')['upload_id']
        with patch('kb_platform.knowledge_platform_contract.monotonic', return_value=901):
            with self.assertRaises(ContractError) as caught:
                case.dependency.ingest_finish({'actor': {'tenant_id': 'tenant', 'principal_id': 'owner'},
                                               'upload_id': identity})
            self.assertEqual(caught.exception.code, 'PLATFORM_TRANSFER_UNAVAILABLE')
        self.begin(case, b'abc')
        case.endpoint.close()
        self.assertEqual(case.endpoint._uploads, {})

    def test_unknown_fields_and_invalid_binary_values_fail_before_platform_dispatch(self):
        case = self.fixture()
        with self.assertRaises(PluginError) as caught:
            case.dependency.create_kb({'actor': {'tenant_id': 'tenant', 'principal_id': 'owner'},
                                      'name': 'Name', '_tx': 'private'})
        self.assertEqual(caught.exception.code, 'CONTRACT_REQUEST_INVALID')
        case.platform.create_kb.assert_not_called()
        identity = self.begin(case, b'abc')['upload_id']
        with self.assertRaises(ContractError) as caught:
            case.dependency.ingest_append({'actor': {'tenant_id': 'tenant', 'principal_id': 'owner'},
                'upload_id': identity, 'offset': 0, 'data_base64': '!!'})
        self.assertEqual(caught.exception.code, 'INVALID_PLATFORM_TRANSFER')
        case.platform.ingest.assert_not_called()
