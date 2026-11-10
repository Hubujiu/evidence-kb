from dataclasses import asdict
from copy import deepcopy
import json
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from kb_platform.agent_model import AgentModel
from kb_platform.agent_luna import LunaModelProvider
from kb_platform.knowledge_contracts import (
    EVIDENCE_METHODS, MODEL_METHODS, SESSION_OPERATIONS, EvidenceClient, EvidenceEndpoint,
    ModelClient, ModelEndpoint, SessionClient, SessionEndpoint, methods,
)
from kb_platform.knowledge_sessions import KnowledgeSessions
from kb_platform.knowledge_answer import KnowledgeAnswer
from kb_platform.model_errors import RemoteModelError
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_native import PluginError
from kb_platform.session_identity import Actor, PlatformError

from .product_fixture import Authority, Gateway, Reranker
from .knowledge_session_fixture import session_fixture


class KnowledgeCapabilityContracts(unittest.TestCase):
    def test_session_json_boundary_preserves_owner_revision_and_idempotency(self):
        with TemporaryDirectory() as directory:
            actor = Actor('tenant', 'reader')
            facade, authority = session_fixture(directory, actor)
            store = facade.store
            self.addCleanup(store.close)
            endpoint = ServiceEndpoint(SessionEndpoint(KnowledgeSessions(store)), methods(SESSION_OPERATIONS))
            client = SessionClient(endpoint)
            actor = Actor('tenant', 'reader')
            session = client.create(actor, 'scope', {'mode': 'AUTO', 'source_ids': [],
                                                   'version_policy': 'latest', 'versions': {}})
            with self.assertRaises(PlatformError) as caught:
                client.load(Actor('tenant', 'other'), session['session_id'])
            self.assertEqual(caught.exception.code, 'SESSION_NOT_FOUND')
            begin = client.begin(actor, session['session_id'], 'request', 0, 'fingerprint')
            completed = client.finish(actor, session['session_id'], begin['token'], session['state'],
                                      {'status': 'answered'}, authority=authority)
            self.assertEqual(completed['revision'], 1)
            self.assertEqual(client.begin(actor, session['session_id'], 'request', 0, 'fingerprint'),
                             {'cached': completed})
            with self.assertRaises(PlatformError) as caught:
                client.begin(actor, session['session_id'], 'new-request', 0, 'other-fingerprint')
            self.assertEqual(caught.exception.code, 'SESSION_REVISION_CONFLICT')

    def gateway(self):
        authority = Authority([('Original.md', 'Original rule value 42.'),
                               ('Second.md', 'Second original rule value 61.')])
        gateway = Gateway(authority, pipeline_reranker=Reranker())
        endpoint = EvidenceEndpoint(gateway)
        self.addCleanup(endpoint.close)
        return authority, endpoint, ServiceEndpoint(endpoint, EVIDENCE_METHODS)

    def prepare(self, authority, endpoint):
        return endpoint.prepare_pipeline({'actor': asdict(authority.actor), 'scope_id': authority.scope,
            'query': 'original', 'policy': {}, 'config': {'channels': ['bm25', 'dense'],
                'rerank': True, 'parent': 'none', 'title_navigation': False}})

    def test_reranking_crosses_json_without_repeating_retrieval_or_embedding(self):
        authority, _, endpoint = self.gateway()
        client = EvidenceClient(endpoint)
        client.pipeline_reranker = Reranker()
        result = client.search_pipeline(authority.actor, authority.scope, 'original', {},
            {'channels': ['bm25', 'dense'], 'rerank': True, 'parent': 'none', 'title_navigation': False})
        self.assertEqual([name for name, _ in authority.calls], ['bm25', 'dense'])
        self.assertEqual(authority.embedding_provider.queries, ['original'])
        self.assertEqual(len(client.pipeline_reranker.inputs), 1)
        self.assertEqual(len(result['hits']), 2)
        self.assertEqual(result['trace']['config']['channels'], ['bm25', 'dense'])

    def test_original_question_survives_serialized_search_hypothesis(self):
        authority, _, endpoint = self.gateway()
        client = EvidenceClient(endpoint)
        client.pipeline_reranker = Reranker()
        client.pipeline_reranker.rank = Mock(wraps=client.pipeline_reranker.rank)
        question = "Where was the spouse of Person A from?"
        result = client.search_pipeline(authority.actor, authority.scope, 'Person B country', {},
            {'channels': ['bm25', 'dense'], 'rerank': True, 'parent': 'none', 'title_navigation': False},
            assessment_question=question)
        self.assertEqual(authority.embedding_provider.queries, ['Person B country'])
        objective = client.pipeline_reranker.rank.call_args.args[0]
        self.assertIn(question, objective)
        self.assertIn('unverified search hypothesis', objective)
        self.assertEqual(result['coverage']['assessment_question'], question)

    def test_rerank_resume_checks_actor_then_current_authority(self):
        authority, _, endpoint = self.gateway()
        prepared = self.prepare(authority, endpoint)
        ranking = [{'citation_id': item['citation_id'], 'score': 1.0} for item in prepared['candidates']]
        with self.assertRaises(ContractError) as caught:
            endpoint.finish_pipeline({'actor': asdict(Actor('tenant', 'other')),
                                      'token': prepared['token'], 'ranking': ranking})
        self.assertEqual(caught.exception.code, 'RETRIEVAL_STAGE_NOT_FOUND')
        authority.revision += 1
        with self.assertRaises(ContractError) as caught:
            endpoint.finish_pipeline({'actor': asdict(authority.actor),
                                      'token': prepared['token'], 'ranking': ranking})
        self.assertEqual(caught.exception.code, 'SCOPE_CHANGED')

    def model(self):
        def post(*args, trace, **kwargs):
            trace['attempt'] += 1
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"count":1}'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

        provider = SimpleNamespace(model='synthetic', base_url='https://example.invalid', retries=0,
            _headers={}, _cache_get=Mock(return_value=None), _cache_put=Mock(), _record=Mock(),
            _save_failed_response=Mock(return_value={}), _post=Mock(side_effect=post))
        model = AgentModel(LunaModelProvider(provider))
        endpoint = ModelEndpoint(model)
        self.addCleanup(endpoint.close)
        return ModelClient(ServiceEndpoint(endpoint, MODEL_METHODS)), model, provider

    def complete(self, client, validator):
        return client.complete([{'role': 'user', 'content': 'Synthetic boundary control'}],
            role='answer', schema={'type': 'object', 'properties': {'count': {'type': 'integer'}},
                                  'required': ['count'], 'additionalProperties': False},
            call_context={}, validator=validator)

    def test_business_rejection_keeps_physical_cost_and_prevents_cache_write(self):
        client, model, provider = self.model()

        def reject(value):
            raise ValueError('EVIDENCE_NOT_SUPPORTED')

        with self.assertRaises(RemoteModelError) as caught:
            self.complete(client, reject)
        self.assertEqual(caught.exception.call_record['attempts'], 1)
        self.assertEqual(caught.exception.call_record['usage']['total_tokens'], 15)
        self.assertEqual(caught.exception.call_record['validator_reason'], 'EVIDENCE_NOT_SUPPORTED')
        self.assertEqual(caught.exception.output_diagnostics['validator_reason'], 'EVIDENCE_NOT_SUPPORTED')
        self.assertEqual(model.ledger.snapshot()['calls'], 1)
        provider._cache_put.assert_not_called()

    def test_only_canonical_refusal_discards_unused_unvalidated_claims(self):
        candidate = {'answer': 'Unverified refusal prose', 'abstain': True,
            'completion': 'insufficient', 'unresolved': ['Missing relation'],
            'claims': [{'text': 'Unused assertion', 'evidence_ids': ['citation'],
                        'quotes': [{'evidence_id': 'citation', 'quote': 'Invented quotation.'}]}]}
        turn = SimpleNamespace(config={'answer_contract': True, 'explicit_completion': True},
            state={'evidence': {'citation': {}}}, evidence=[{}],
            model_evidence={'answer': {'citation': 'Original text.'}}, model_user_statements={},
            trace={'route': 'knowledge'})
        KnowledgeAnswer.validate_answer(turn, candidate)
        self.assertEqual(turn.trace['discarded_refusal_candidates'][0]['claims'], 1)
        self.assertEqual(candidate['claims'][0]['quotes'][0]['quote'], 'Invented quotation.')
        for canonical, abstain, completion in [(False, True, 'insufficient'),
                (True, False, 'partial'), (True, False, 'complete')]:
            with self.subTest(canonical=canonical, completion=completion):
                turn.config['answer_contract'] = canonical
                value = {**candidate, 'abstain': abstain, 'completion': completion,
                         'unresolved': [] if completion == 'complete' else ['Missing relation']}
                with self.assertRaisesRegex(ValueError, 'INVALID_EXACT_QUOTE'):
                    KnowledgeAnswer.validate_answer(turn, value)

    def test_citation_repair_survives_model_contract_validation_boundary(self):
        client, model, provider = self.model()
        candidate = {'answer': 'Verified fact.', 'claims': [{'text': 'Verified fact.',
            'evidence_ids': ['citation'], 'quotes': [{'evidence_id': 'citation', 'quote': 'Changed fact.'}]}]}
        corrected = deepcopy(candidate)
        corrected['claims'][0]['quotes'][0]['quote'] = 'Verified fact.'
        values = iter((candidate, corrected))

        def post(*args, trace, **kwargs):
            trace['attempt'] += 1
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(next(values))}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

        provider._post.side_effect = post
        turn = SimpleNamespace(config={'repair_citations': True}, citation_repair_used=False,
            message='What is verified?', answer_schema={'type': 'object'},
            state={'evidence': {'citation': {}}}, evidence=[{}],
            model_evidence={'answer': {'citation': 'Verified fact.'}}, model_user_statements={},
            trace={'calls': [], 'route': 'knowledge'}, event=Mock())
        turn.validate_answer = lambda value: KnowledgeAnswer.validate_answer(turn, value)

        def call(role, system, schema, validator, *, question):
            if turn.citation_repair_used:
                self.assertIn('Local validation location:', system)
                self.assertIn('exact_substring_of_current_visible_original', system)
                self.assertIn('"path":["claims",0,"quotes",0]', system)
            try:
                return client.complete([{'role': 'system', 'content': system},
                    {'role': 'user', 'content': question}], role=role, schema=schema,
                    call_context={}, validator=validator).value
            except RemoteModelError as error:
                turn.trace['calls'].append({**error.call_record, 'call_id': error.call_record['request_id']})
                raise

        turn.call = call
        self.assertEqual(KnowledgeAnswer.call_answer(turn, 'Use exact evidence.'), corrected)
        self.assertEqual(turn.trace['citation_repair']['reason'], 'INVALID_EXACT_QUOTE')
        self.assertEqual(turn.trace['citation_repair']['validator_issue']['evidence_id'], 'citation')
        self.assertTrue(turn.citation_repair_used)
        self.assertEqual(provider._post.call_count, 2)
        self.assertEqual(model.ledger.snapshot()['calls'], 2)
        provider._cache_put.assert_called_once()

    def test_business_validation_acknowledges_exact_completion_before_caching(self):
        client, model, provider = self.model()

        def accept(value):
            self.assertEqual(value, {'count': 1})
            provider._cache_put.assert_not_called()

        result = self.complete(client, accept)
        self.assertEqual(result.value, {'count': 1})
        self.assertEqual(model.ledger.snapshot()['calls'], 1)
        provider._cache_put.assert_called_once()

    def test_unknown_validation_ack_keeps_received_usage_without_repeating_model_or_settlement(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                client, model, provider = self.model()
                original = client._dependency
                settlements = []

                class UncertainEndpoint:
                    def __getattr__(self, name):
                        return getattr(original, name)

                    def settle(self, request):
                        settlements.append(request)
                        raise PluginError('PROCESS_RESULT_UNKNOWN')

                client = ModelClient(UncertainEndpoint())

                def validate(value):
                    if not valid:
                        raise ValueError('EVIDENCE_NOT_SUPPORTED')

                with self.assertRaises(PluginError if valid else RemoteModelError) as caught:
                    self.complete(client, validate)
                error = caught.exception
                self.assertEqual(error.code, 'PROCESS_RESULT_UNKNOWN' if valid else 'INVALID_AGENT_OUTPUT')
                record = error.call_record
                self.assertEqual(record['attempts'], 1)
                self.assertEqual(record['usage']['total_tokens'], 15)
                self.assertTrue(record['request_id'])
                self.assertEqual(record['cache_settlement_error'], {
                    'code': 'PROCESS_RESULT_UNKNOWN', 'operation': 'accept' if valid else 'reject', 'retried': False})
                if not valid:
                    self.assertEqual(record['validator_reason'], 'EVIDENCE_NOT_SUPPORTED')
                self.assertEqual(len(settlements), 1)
                self.assertEqual(model.ledger.snapshot()['calls'], 1)
                provider._post.assert_called_once()
                provider._cache_put.assert_not_called()

    def test_semantic_query_keeps_physical_receipt_and_missing_index_distinction(self):
        from kb_platform.knowledge_semantic_contract import METHODS, SemanticClient, SemanticEndpoint
        from kb_platform.semantic_pages_v2 import SemanticPageResults

        actor = Actor('tenant', 'reader')
        receipt = {'embedding_request_id': 'physical-query-1', 'embedding_input_tokens': 17,
                   'semantic_manifest_sha256': 'synthetic-checksum'}
        service = SimpleNamespace(current_manifest=lambda actor, kb_id: ({'build_id': 'build'}, 'checksum'),
            query=lambda actor, **request: SemanticPageResults([{'citation_id': 'original-page'}], receipt))
        client = SemanticClient(ServiceEndpoint(SemanticEndpoint(service), METHODS))
        self.assertEqual(client.current_manifest(actor, 'scope'), ({'build_id': 'build'}, 'checksum'))
        result = client.query(actor, 'scope', 'original', {})
        self.assertEqual(result, [{'citation_id': 'original-page'}])
        self.assertEqual(result.receipt, receipt)
        for cause in (FileNotFoundError('missing'), ValueError('corrupt')):
            def fail(actor, kb_id, cause=cause):
                raise PlatformError('SEMANTIC_PAGE_INDEX_UNAVAILABLE', 'Unavailable', 409) from cause
            service.current_manifest = fail
            with self.assertRaises(PlatformError) as caught:
                client.current_manifest(actor, 'scope')
            self.assertEqual(isinstance(caught.exception.__cause__, FileNotFoundError),
                             isinstance(cause, FileNotFoundError))

    def test_foreign_model_metadata_missing_required_fields_fails_at_service_boundary(self):
        _, model, _ = self.model()
        baseline = ModelEndpoint(model).metadata({})
        for field in ('policy', 'capabilities', 'provider_id', 'manifest', 'max_attempts'):
            with self.subTest(field=field):
                malformed = deepcopy(baseline)
                del malformed[field]
                implementation = ModelEndpoint(model)
                implementation.metadata = lambda request, value=malformed: value
                with self.assertRaises(PluginError) as caught:
                    ModelClient(ServiceEndpoint(implementation, MODEL_METHODS))
                self.assertEqual(caught.exception.code, 'CONTRACT_RESPONSE_INVALID')
        malformed = deepcopy(baseline)
        malformed['policy']['input_budget'] = 'untyped'
        implementation.metadata = lambda request: malformed
        with self.assertRaises(PluginError) as caught:
            ModelClient(ServiceEndpoint(implementation, MODEL_METHODS))
        self.assertEqual(caught.exception.code, 'CONTRACT_RESPONSE_INVALID')

    def test_foreign_prepared_context_validates_consumed_fields_before_workflow(self):
        from kb_platform.knowledge_capabilities import registry
        from .knowledge_context_contracts import KnowledgeContextContracts

        case = KnowledgeContextContracts().fixture()
        expected = case.context.prepare_request(case.request)
        declarations = registry({}).methods({'id': 'knowledge.context', 'major': 1})
        malformed_paths = [('prepared',), ('prepared', 'messages'), ('prepared', 'budget', 'input_limit'),
            ('prepared', 'estimate', 'tokens'), ('prepared', 'estimate', 'kind'),
            ('prepared', 'compaction_needed'), ('prepared', 'projection', 'evidence'),
            ('prepared', 'projection', 'state')]
        for path in malformed_paths:
            with self.subTest(path=path):
                response = deepcopy(expected)
                parent = response
                for name in path[:-1]:
                    parent = parent[name]
                del parent[path[-1]]
                implementation = SimpleNamespace(prepare_request=lambda request: response,
                    sources=lambda request: [], metadata=lambda request: {})
                endpoint = ServiceEndpoint(implementation, declarations)
                with self.assertRaises(PluginError) as caught:
                    endpoint.prepare_request(case.request)
                self.assertEqual(caught.exception.code, 'CONTRACT_RESPONSE_INVALID')
        response = deepcopy(expected)
        response['prepared']['projection']['vendor_extension'] = {'open_domain_value': True}
        response['prepared']['estimate']['vendor_measurement'] = {'measured': 19}
        implementation.prepare_request = lambda request: response
        self.assertEqual(ServiceEndpoint(implementation, declarations).prepare_request(case.request), response)
