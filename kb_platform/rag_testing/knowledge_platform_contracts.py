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
        self.endpoint, self.requests = endpoint, []

    def __getattr__(self, name):
        operation = getattr(self.endpoint, name)

        def call(request):
            self.requests.append((name, json.loads(json.dumps(request))))
            return operation(request)
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
            read_original=Mock(return_value=b'original'))
        endpoint = PlatformEndpoint(platform)
        dependency = RecordingDependency(ServiceEndpoint(endpoint, METHODS))
        client = PlatformClient(dependency)
        self.addCleanup(endpoint.close)
        return SimpleNamespace(platform=platform, endpoint=endpoint, dependency=dependency,
                               client=client, actor=Actor('tenant', 'owner'))

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
        transfers = [(name, request) for name, request in case.dependency.requests if name in {
            'ingest_append', 'read_original_chunk'}]
        self.assertGreater(len(transfers), 2)
        self.assertTrue(all(len(json.dumps(request).encode()) < 4 * 1024 * 1024 for _, request in transfers))
        self.assertEqual(case.endpoint._uploads, {})

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
        case.platform.read_original.side_effect = [b'a' * (CHUNK_BYTES + 5), b'b' * (CHUNK_BYTES + 5)]
        with self.assertRaises(PlatformError) as caught:
            case.client.read_original(case.actor, 'kb-a', 'citation-a')
        self.assertEqual(caught.exception.code, 'SOURCE_INTEGRITY')

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
