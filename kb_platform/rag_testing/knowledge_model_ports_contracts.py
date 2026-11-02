import base64
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx

from kb_platform.embedding_jobs import classify_provider_failure, retry_delay
from kb_platform.embeddings import EmbeddingCallError
from kb_platform.knowledge_model_ports import AuxiliaryModelEndpoint, EmbeddingClient, METHODS, OCRClient
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from kb_platform.ocr_contract import OCR_CONTRACT_VERSION, validate_ocr_response
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_native import PluginError
from . import luna_sdk_contracts, model_embedding_review_contracts


OCR_RESULT = {'blocks': [{'text': 'Verified text', 'bbox': [0, 0, 1, 1]}],
              'complete': True, 'unreadable': []}


class RecordedDependency:
    def __init__(self, endpoint):
        self.endpoint, self.requests = endpoint, []

    def __getattr__(self, name):
        target = getattr(self.endpoint, name)

        def call(request):
            self.requests.append((name, json.loads(json.dumps(request))))
            return target(request)
        return call


class KnowledgeModelPortsContracts(unittest.TestCase):
    def fixture(self, *, embedding=None, ocr=None, authorization=None):
        endpoint = AuxiliaryModelEndpoint(embedding=embedding, ocr=ocr, authorization=authorization)
        self.addCleanup(endpoint.close)
        dependency = RecordedDependency(ServiceEndpoint(endpoint, METHODS))
        metadata = dependency.metadata({})
        return SimpleNamespace(endpoint=endpoint, dependency=dependency,
            ocr=OCRClient(dependency, metadata['ocr']) if ocr is not None else None,
            embedding=EmbeddingClient(dependency, metadata['embedding']) if embedding is not None else None)

    def ocr_provider(self):
        def complete(system, user, *, purpose, images, validator, contract_version):
            validator(OCR_RESULT)
            return deepcopy(OCR_RESULT)
        return SimpleNamespace(provider_id='ocr-fixture', model='fixed', manifest={},
            maximum_format_corrections=0, complete_json=Mock(side_effect=complete))

    def embedding_provider(self):
        return SimpleNamespace(provider_id='embedding-fixture', model='fixed', dimension=2,
            max_batch_size=4, manifest={}, documents=Mock(return_value=[[1., 0.]]),
            query=Mock(return_value=[1., 0.]), recover_embedding=Mock(return_value={'physical_status': 'UNKNOWN'}),
            embedding_preflight=Mock(side_effect=lambda request_id, error: error.preflight_code))

    def call_ocr(self, client, image=b'\x89PNG\r\n\x1a\nsynthetic'):
        return client.complete_json('Read source', 'Read image', purpose='ocr', images=[image],
                                    validator=validate_ocr_response, contract_version=OCR_CONTRACT_VERSION)

    def test_large_ocr_image_is_transferred_in_chunks_without_serializing_validator(self):
        provider = self.ocr_provider()
        case = self.fixture(ocr=provider)
        data = b'\x89PNG\r\n\x1a\n' + b'a' * (9 * 1024 * 1024)
        self.assertEqual(self.call_ocr(case.ocr, data), OCR_RESULT)
        self.assertEqual(provider.complete_json.call_count, 1)
        options = provider.complete_json.call_args.kwargs
        self.assertEqual(options['images'], [data])
        self.assertIs(options['validator'], validate_ocr_response)
        self.assertEqual(options['contract_version'], OCR_CONTRACT_VERSION)
        self.assertTrue(all(len(json.dumps(request).encode()) < 4 * 1024 * 1024
                            for _, request in case.dependency.requests))
        self.assertEqual(case.endpoint._uploads, {})
        self.assertEqual(case.ocr.maximum_format_corrections, 0)
        self.assertFalse(hasattr(case.ocr, 'snapshot_stats'))

    def test_provider_validates_ocr_before_cache_and_keeps_single_format_correction(self):
        state = luna_sdk_contracts.LunaSDKContracts.setup_provider(self, [
            luna_sdk_contracts.completion('{"invalid":true}'),
            luna_sdk_contracts.completion(json.dumps(OCR_RESULT))], cache_mode='read_write')
        case = self.fixture(ocr=state.provider)
        self.assertEqual(self.call_ocr(case.ocr), OCR_RESULT)
        self.assertEqual(self.call_ocr(case.ocr), OCR_RESULT)
        self.assertEqual(len(state.requests), 2)
        self.assertEqual(state.ledger.summary()['physical_requests'], 2)
        self.assertEqual(case.ocr.maximum_format_corrections, 1)
        self.assertEqual(case.ocr.snapshot_stats()['by_purpose']['ocr']['requests'], 2)

    def test_terminal_unreadable_ocr_output_never_triggers_an_extra_generation(self):
        terminal = {'complete': False, 'unreadable': ['bottom half'],
                    'blocks': [{'text': '', 'bbox': [0, 0, 1, 1]}]}
        state = luna_sdk_contracts.LunaSDKContracts.setup_provider(self, [
            luna_sdk_contracts.completion(json.dumps(terminal))])
        case = self.fixture(ocr=state.provider)
        with self.assertRaises(RemoteModelError) as caught:
            self.call_ocr(case.ocr)
        self.assertEqual(caught.exception.code, 'INVALID_MODEL_OUTPUT')
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(case.endpoint._uploads, {})

    def test_ocr_transfer_rejects_corruption_and_unknown_contract_before_model_send(self):
        provider = self.ocr_provider()
        case = self.fixture(ocr=provider)
        request = {'system': 'Read', 'user': 'Image', 'purpose': 'ocr', 'image_size': 3,
                   'image_sha256': sha256(b'abc').hexdigest(), 'contract_version': OCR_CONTRACT_VERSION}
        with self.assertRaises(PluginError):
            case.dependency.ocr_begin({**request, 'contract_version': 'private/1'})
        transfer = case.dependency.ocr_begin(request)
        case.dependency.ocr_append({**transfer, 'offset': 0, 'data_base64': base64.b64encode(b'abd').decode()})
        with self.assertRaises(ContractError) as caught:
            case.dependency.ocr(transfer)
        self.assertEqual(caught.exception.code, 'INVALID_OCR_TRANSFER')
        provider.complete_json.assert_not_called()
        self.assertTrue(case.dependency.ocr_cancel(transfer)['cancelled'])

    def test_embedding_preflight_and_recovery_preserve_failure_identity_without_resend(self):
        provider = self.embedding_provider()
        case = self.fixture(embedding=provider)
        cause = ModelPreflightError('BUDGET_INSUFFICIENT')
        provider.documents.side_effect = EmbeddingCallError(cause)
        with self.assertRaises(EmbeddingCallError) as caught:
            case.embedding.documents(['text'], request_id='job-a')
        error = caught.exception
        self.assertEqual(error.preflight_code, 'BUDGET_INSUFFICIENT')
        self.assertIs(error.model_sent, False)
        self.assertEqual(classify_provider_failure({'request_id': 'job-a'}, case.embedding, error).code,
                         'EMBEDDING_BUDGET_INSUFFICIENT')
        self.assertEqual(case.embedding.embedding_preflight('job-a', error), 'BUDGET_INSUFFICIENT')
        observed = provider.embedding_preflight.call_args.args[1]
        self.assertIsInstance(observed, EmbeddingCallError)
        self.assertEqual(observed.preflight_code, 'BUDGET_INSUFFICIENT')
        self.assertEqual(case.embedding.recover_embedding('job-a'), {'physical_status': 'UNKNOWN'})
        self.assertEqual(provider.documents.call_count, 1)

    def test_embedding_transport_failure_keeps_status_retry_hint_and_physical_request(self):
        fixtures = model_embedding_review_contracts.ModelFixtures
        fixtures.setUp(self)
        adapter = fixtures.adapter(self, respond=lambda request: httpx.Response(
            429, json={'error': 'throttled'}, headers={'Retry-After': '19'}))
        view, _ = fixtures.view(self, adapter)
        case = self.fixture(embedding=view)
        with self.assertRaises(EmbeddingCallError) as caught:
            case.embedding.documents(['private text'], request_id='job-physical')
        error = caught.exception
        self.assertEqual(error.status, 429)
        self.assertEqual(retry_delay(error, 1), 19)
        self.assertTrue(error.model_sent)
        self.assertTrue(error.physical_request_id)
        self.assertEqual(len(self.sent), 1)

    def test_embedding_budget_preflight_keeps_unsent_state_without_new_physical_request(self):
        fixtures = model_embedding_review_contracts.ModelFixtures
        fixtures.setUp(self)
        view, _ = fixtures.view(self, fixtures.adapter(self))
        case = self.fixture(embedding=view)
        self.assertEqual(case.embedding.documents(['first'], request_id='first'), [[1., 0.]])
        with self.assertRaises(EmbeddingCallError) as caught:
            case.embedding.documents(['second'], request_id='second')
        self.assertEqual(caught.exception.preflight_code, 'BUDGET_INSUFFICIENT')
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(len(self.sent), 1)

    def test_embedding_authorization_remains_local_and_checks_before_and_after_the_call(self):
        provider = self.embedding_provider()
        authority = SimpleNamespace(validate_embedding=Mock(return_value={'authorized': True}))
        case = self.fixture(embedding=provider, authorization=authority)
        with self.assertRaisesRegex(ValueError, 'EMBEDDING_AUTHORIZATION_FAILED'):
            case.embedding.query('private text', authorize=lambda: False)
        provider.query.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'EMBEDDING_AUTHORIZATION_FAILED'):
            case.embedding.query('private text', request_id='job-a', audit_context={'scope': 'scope-a'},
                                 authorize=Mock(side_effect=[True, False]))
        self.assertEqual(provider.query.call_args.args, ('private text',))
        options = provider.query.call_args.kwargs
        self.assertEqual(options['request_id'], 'job-a')
        self.assertEqual(options['audit_context'], {'scope': 'scope-a'})
        self.assertTrue(options['authorize']())
        authority.validate_embedding.assert_called_once_with({'audit_context': {'scope': 'scope-a'},
            'task': 'query', 'provider_id': 'embedding-fixture'})
        self.assertNotIn('authorize', json.dumps(case.dependency.requests))

    def test_embedding_provider_rechecks_public_authorization_after_reservation(self):
        from kb_platform.model_adapters import EmbeddingAdapter

        fixtures = model_embedding_review_contracts.ModelFixtures
        fixtures.setUp(self)
        provider = fixtures.observed(self)
        view, _ = fixtures.view(self, EmbeddingAdapter(provider), dimension=768)
        access = SimpleNamespace(allowed=True, requests=[])

        def validate(request):
            access.requests.append(json.loads(json.dumps(request)))
            return {'authorized': access.allowed}

        access.validate_embedding = validate
        case = self.fixture(embedding=view, authorization=access)
        reserve = provider.ledger.reserve

        def revoke(*args, **kwargs):
            result = reserve(*args, **kwargs)
            access.allowed = False
            return result

        provider.ledger.reserve = revoke
        with self.assertRaises(EmbeddingCallError):
            case.embedding.documents(['source text'], request_id='revoked-before-send',
                audit_context=fixtures.audit(), authorize=lambda: True)
        self.assertEqual(self.sent, [])
        self.assertGreaterEqual(len(access.requests), 2)
        self.assertEqual(access.requests[-1], {'audit_context': fixtures.audit(),
            'task': 'document', 'provider_id': provider.provider_id})
        self.assertEqual(provider.ledger.summary()['pending_or_unknown'], 0)

    def test_audited_embedding_fails_closed_without_public_authorization_service(self):
        provider = self.embedding_provider()
        case = self.fixture(embedding=provider)
        from kb_platform.session_identity import PlatformError

        with self.assertRaises(PlatformError) as caught:
            case.embedding.documents(['source'], audit_context={'tenant_id': 'tenant'}, authorize=lambda: True)
        self.assertEqual(caught.exception.code, 'EMBEDDING_AUTHORIZATION_UNAVAILABLE')
        provider.documents.assert_not_called()

    def test_observed_embedding_uses_json_authorization_and_recovers_the_original_request(self):
        from kb_platform.model_adapters import EmbeddingAdapter

        fixtures = model_embedding_review_contracts.ModelFixtures
        fixtures.setUp(self)
        provider = fixtures.observed(self)
        view, _ = fixtures.view(self, EmbeddingAdapter(provider), dimension=768)
        access = SimpleNamespace(validate_embedding=Mock(return_value={'authorized': True}))
        case = self.fixture(embedding=view, authorization=access)
        result = case.embedding.documents(['source text'], request_id='observed-wire',
            audit_context=fixtures.audit(), authorize=lambda: True)
        capture = case.embedding.recover_embedding('observed-wire')
        self.assertEqual(capture['physical_status'], 'COMPLETE')
        self.assertEqual(capture['record']['vectors'], result)
        self.assertEqual(capture['record']['physical_request_id'], 'observed-wire')
        self.assertEqual(capture['record']['audit_context'], fixtures.audit())
        self.assertGreaterEqual(access.validate_embedding.call_count, 3)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_wraps_preflight_failures_as_embedding_errors(self):
        provider = self.embedding_provider()
        provider.recover_embedding.side_effect = EmbeddingCallError(ModelPreflightError('MODEL_BINDING_CHANGED'))
        case = self.fixture(embedding=provider)
        with self.assertRaises(EmbeddingCallError) as caught:
            case.embedding.recover_embedding('job-a')
        self.assertEqual(caught.exception.preflight_code, 'MODEL_BINDING_CHANGED')
