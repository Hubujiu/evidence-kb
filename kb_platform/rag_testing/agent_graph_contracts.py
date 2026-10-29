from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from kb_platform.agent_luna import LunaModelProvider
from kb_platform.agent_model import AgentModel, CompletionEnvelope
from kb_platform.agent_schema import compile_schema, validate_output
from kb_platform.knowledge_service import KnowledgeService as AgentService
from kb_platform.knowledge_policy import CONFIGS, RECOMMENDED
from kb_platform.core import Actor, PlatformError
from .budget import BudgetLedger
from .product_fixture import Authority, Gateway
from .provider_capture import CapturedLuna, observe_client


def decision(action='search', **fields):
    return {'intent': 'knowledge', 'action': action, 'query': 'original', **fields}


def verdict(supported=True, complete=True):
    return {'supported': supported, 'reason': 'synthetic review', 'task_complete': complete,
            'missing_information': [] if complete else ['unresolved part']}


def cited_answer(messages):
    projection = json.loads(messages[-1]['content'])
    item = next(row for row in projection['evidence'] if row.get('text'))
    return {'answer': item['text'], 'abstain': False, 'completion': 'complete', 'unresolved': [],
            'claims': [{'text': item['text'], 'evidence_ids': [item['citation_id']],
                        'quotes': [{'evidence_id': item['citation_id'], 'quote': item['text']}]}]}


def normal_script(review=None):
    return [('decision', decision()), ('retrieval', decision('answer')), ('answer', cited_answer),
            ('verifier', review if review is not None else verdict())]


class ScenarioModel:
    def __init__(self, script, after=None):
        self.script, self.calls, self.after = list(script), [], after

    def complete(self, messages, *, role, schema, validator, **kwargs):
        self.calls.append({'role': role, 'messages': deepcopy(messages)})
        expected_role, value = self.script.pop(0)
        if role != expected_role:
            raise AssertionError(f'Expected {expected_role}, got {role}')
        if isinstance(value, BaseException):
            raise value
        value = value(messages) if callable(value) else deepcopy(value)
        validate_output(value, compile_schema(schema))
        validator(value)
        if self.after:
            self.after(role, value)
        return CompletionEnvelope(value, kwargs['call_context']['call_id'],
            {'available': True, 'total_tokens': 10, 'prompt_tokens': 7, 'completion_tokens': 3},
            1, 'stop', 'none', 'synthetic-request')


class AgentGraphContracts(unittest.TestCase):
    def setup_service(self, script=(), *, after=None, policy=None, config=None):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        authority = Authority([('Original.md', 'Original rule: value 42.'), ('Other.md', 'Other private rule.')])
        gateway = Gateway(authority)
        gateway.authorize = lambda actor, scope: {'scope_id': scope, 'manifest_id': 'build',
                                                 'authority_revision': authority.revision}
        gateway.sources = lambda actor, scope: [{'source_id': r['source_id'], 'doc_id': r['source_id'],
            'source_version_id': r['source_version_id'], 'title': r['filename']} for r in authority.rows]
        model = ScenarioModel(script, after)
        if config:
            changed = patch.dict(CONFIGS, {RECOMMENDED: {**CONFIGS[RECOMMENDED], **config}})
            changed.start()
            self.addCleanup(changed.stop)
        service = AgentService(None, model, root=root, gateway=gateway,
                               retrieval_config={'channels': ['bm25'], 'rerank': False})
        session = service.create(authority.actor, authority.scope, policy=policy)
        return SimpleNamespace(service=service, model=model, authority=authority, gateway=gateway,
                               root=root, session=session['session_id'], captures=[])

    def turn(self, case, message='What is the original rule?', request='turn', revision=0):
        return case.service.turn(case.authority.actor, case.session, message=message,
                                 request_id=request, expected_revision=revision)

    def events(self, case):
        return case.service.store.events(case.authority.actor, case.session)

    def test_normal_graph_delivers_originals_and_durable_node_events(self):
        case = self.setup_service(normal_script())
        case.service.capture = case.captures.append
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['answer'], 'Original rule: value 42.')
        self.assertEqual(result['trace']['graph']['nodes'], ['plan', 'tool', 'plan', 'compose', 'verify', 'deliver'])
        self.assertEqual([e['payload']['node'] for e in self.events(case) if e['kind'] == 'graph_node_started'],
                         result['trace']['graph']['nodes'])
        self.assertEqual([c['role'] for c in case.model.calls], ['decision', 'retrieval', 'answer', 'verifier'])
        answer_capture = next(row for row in case.captures if row['role'] == 'answer')
        self.assertIn('Original rule: value 42.', json.dumps(answer_capture['prepared'].messages))
        self.assertTrue(result['claims'][0]['quotes'])
        self.assertFalse(case.model.script)

    def test_greeting_and_calculation_need_no_model(self):
        for message, answer, nodes in (
            ('你好', '你好。请选择知识库并告诉我需要查找的内容；也可以指定文档范围。', ['plan', 'compose', 'deliver']),
            ('计算: 2+3', '5', ['plan', 'tool', 'compose', 'deliver']),
        ):
            with self.subTest(message=message):
                case = self.setup_service()
                result = self.turn(case, message)
                self.assertEqual(result['answer'], answer)
                self.assertEqual(result['trace']['graph']['nodes'], nodes)
                self.assertEqual(case.model.calls, [])

    def test_clarification_and_empty_evidence_stop_before_generation(self):
        case = self.setup_service([('decision', decision('clarify', clarification='Which source?'))])
        result = self.turn(case)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(result['answer'], 'Which source?')
        case = self.setup_service([('decision', decision()), ('retrieval', decision('answer'))])
        case.gateway.search_pipeline = lambda *args, **kwargs: {'hits': [], 'trace': {'expansion': []}}
        result = self.turn(case)
        self.assertEqual(result['status'], 'abstained', result)
        self.assertEqual(result['stop_reason'], 'no_evidence')
        self.assertEqual(len(case.model.calls), 2)

    def test_repeated_tool_arguments_stop_without_reexecuting(self):
        case = self.setup_service([('decision', decision()), ('retrieval', decision()),
                                   ('answer', cited_answer), ('verifier', verdict())])
        result = self.turn(case)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(result['stop_reason'], 'repeated_action')
        self.assertEqual(len(result['trace']['tool_calls']), 1)

    def test_default_ten_step_limit_routes_to_partial_delivery(self):
        script = [('decision' if i == 0 else 'retrieval', decision(query=f'query {i}')) for i in range(10)]
        case = self.setup_service(script + [('answer', cited_answer), ('verifier', verdict())])
        result = self.turn(case)
        self.assertEqual(result['status'], 'partial', result)
        self.assertEqual(result['stop_reason'], 'step_limit')
        self.assertEqual(len(result['trace']['tool_calls']), 10)
        self.assertEqual(len(case.model.calls), 12)

    def test_tool_failure_is_not_retried_by_graph(self):
        case = self.setup_service([('decision', decision())])
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            raise PlatformError('INVALID_SOURCE', 'synthetic', 400)
        case.authority.backend.search = fail
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'tool_failure')
        self.assertEqual(calls, [1])
        self.assertEqual(len(case.model.calls), 1)

    def test_workflow_missing_state_routes_directly_to_clarification(self):
        def workflow_decision(messages):
            evidence = json.loads(messages[-1]['content'])['evidence']
            return decision('workflow_next', citation_ids=[evidence[0]['citation_id']])
        case = self.setup_service([('decision', decision()), ('retrieval', workflow_decision)])
        def workflow(actor, scope, citation_id, policy, **kwargs):
            return {'status': 'clarify', 'missing_info': ['当前步骤'],
                    'evidence': case.gateway.read(actor, scope, [citation_id], policy)[0]}
        case.gateway.workflow_next = workflow
        result = self.turn(case)
        self.assertEqual(result['status'], 'clarification', result)
        self.assertIn('当前步骤', result['answer'])
        self.assertEqual(result['trace']['graph']['nodes'], ['plan', 'tool', 'plan', 'tool', 'compose', 'deliver'])
        self.assertEqual(len(case.model.calls), 2)

    def test_verification_repair_is_bounded_and_reverified(self):
        for supported in (True, False):
            with self.subTest(supported=supported):
                case = self.setup_service(normal_script(verdict(False)) +
                    [('answer', cited_answer), ('verifier', verdict(supported))])
                result = self.turn(case)
                self.assertEqual(result['status'], 'answered' if supported else 'abstained', result)
                self.assertEqual(result['trace']['graph']['nodes'][-5:], ['compose', 'verify', 'repair', 'verify', 'deliver'])
                self.assertEqual(result['trace']['repair']['attempts'], 1)
                self.assertEqual(len(case.model.calls), 6)
                if not supported:
                    self.assertEqual(result['claims'], [])
                    self.assertNotIn('value 42', result['answer'])

    def test_supported_partial_verdict_remains_partial(self):
        case = self.setup_service(normal_script(verdict(complete=False)))
        result = self.turn(case)
        self.assertEqual(result['status'], 'partial', result)
        self.assertIn('unresolved part', result['unresolved'])
        self.assertNotIn('repair', result['trace']['graph']['nodes'])

    def test_repair_tool_failure_retains_receipt_and_redacts_derived_text(self):
        case = self.setup_service(normal_script(verdict(False)))
        searches = []
        def fail(*args, **kwargs):
            searches.append(1)
            raise PlatformError('SOURCE_UNAVAILABLE', 'synthetic repair failure', 503)
        case.gateway.search = fail
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'SOURCE_UNAVAILABLE')
        self.assertEqual(searches, [1])
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['answer'], '')
        self.assertNotIn('value 42', json.dumps(result))
        receipts = [e for e in self.events(case) if e['kind'] == 'tool_result']
        self.assertEqual(receipts[-1]['payload']['status'], 'error')
        self.assertEqual(receipts[-1]['group_id'], 'turn:repair:0')

    def test_disabled_repair_abstains_without_another_generation(self):
        case = self.setup_service(normal_script(verdict(False)), config={'repair': False})
        result = self.turn(case)
        self.assertEqual(result['status'], 'abstained', result)
        self.assertEqual(result['stop_reason'], 'verification_failed')
        self.assertNotIn('repair', result['trace']['graph']['nodes'])
        self.assertEqual(len(case.model.calls), 4)

    def test_retryable_node_exception_is_not_automatically_retried(self):
        case = self.setup_service([('decision', ConnectionError('synthetic failure'))])
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'ConnectionError')
        self.assertEqual(len(case.model.calls), 1)

    def test_permission_change_before_verification_or_delivery_redacts_response(self):
        for change_at, supported in (('answer', True), ('verifier', True), ('verifier', False)):
            with self.subTest(change_at=change_at, supported=supported):
                case = self.setup_service(normal_script(verdict(supported)))
                def revoke(role, value):
                    if role == change_at:
                        case.authority.revision += 1
                case.model.after = revoke
                result = self.turn(case)
                self.assertEqual(result['status'], 'error', result)
                self.assertEqual(result['stop_reason'], 'SCOPE_CHANGED')
                self.assertEqual(result['answer'], '')
                self.assertEqual(result['evidence'], [])
                self.assertEqual(result['claims'], [])
                self.assertNotIn('value 42', json.dumps(result))

    def test_repair_state_is_isolated_between_sessions(self):
        script = normal_script(verdict(False)) + [('answer', cited_answer), ('verifier', verdict())]
        case = self.setup_service(script + script)
        first = self.turn(case)
        case.session = case.service.create(case.authority.actor, case.authority.scope)['session_id']
        second = self.turn(case)
        for result in (first, second):
            self.assertEqual(result['status'], 'answered', result)
            self.assertEqual(result['trace']['graph']['nodes'].count('repair'), 1)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(len(case.model.calls), 12)

    def test_locked_sources_and_cached_answer_are_reauthorized(self):
        case = self.setup_service(normal_script(), policy={'mode': 'LOCKED', 'source_ids': ['s0']})
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual({e['source_id'] for e in result['evidence']}, {'s0'})
        self.assertNotIn('Other private rule', json.dumps(case.model.calls))
        self.assertEqual(self.turn(case), json.loads(json.dumps(result)))
        self.assertEqual(len(case.model.calls), 4)
        case.authority.rows[0]['policy']['llm_context'] = False
        with self.assertRaises(PlatformError):
            self.turn(case)
        self.assertEqual(len(case.model.calls), 4)

    def test_insufficient_final_budget_blocks_exploration_and_preserves_failed_request(self):
        case = self.setup_service(normal_script(), config={'max_model_calls': 2})
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'AGENT_BUDGET_EXHAUSTED')
        self.assertEqual(len(case.model.calls), 0)
        self.assertEqual(self.turn(case), result)
        self.assertEqual(len(case.model.calls), 0)

    def test_elapsed_budget_stops_before_tool_node(self):
        case = self.setup_service([('decision', decision())])
        clock = [0.0]
        case.model.after = lambda role, value: clock.__setitem__(0, 1000.0)
        with patch('kb_platform.knowledge_turn.time.perf_counter', side_effect=lambda: clock[0]):
            result = self.turn(case)
        self.assertEqual(result['stop_reason'], 'AGENT_BUDGET_EXHAUSTED')
        self.assertEqual(case.authority.calls, [])

    def test_session_owner_revision_fingerprint_and_single_writer_remain_enforced(self):
        case = self.setup_service(normal_script())
        with self.assertRaises(PlatformError) as caught:
            case.service.turn(Actor('tenant', 'intruder'), case.session, message='question',
                              request_id='turn', expected_revision=0)
        self.assertEqual(caught.exception.code, 'SESSION_NOT_FOUND')
        with self.assertRaises(PlatformError) as caught:
            self.turn(case, revision=1)
        self.assertEqual(caught.exception.code, 'SESSION_REVISION_CONFLICT')
        competing_codes = []
        def competing_turn(role, value):
            if role == 'decision':
                try:
                    self.turn(case, request='competing')
                except PlatformError as exc:
                    competing_codes.append(exc.code)
        case.model.after = competing_turn
        self.assertEqual(self.turn(case)['status'], 'answered')
        self.assertEqual(competing_codes, ['TURN_IN_PROGRESS'])
        with self.assertRaises(PlatformError) as caught:
            self.turn(case, message='Changed input')
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')

    def test_cancelled_node_is_not_replayed_after_service_restart(self):
        case = self.setup_service([('decision', KeyboardInterrupt('synthetic interruption'))])
        with self.assertRaises(KeyboardInterrupt):
            self.turn(case)
        case.service = AgentService(None, case.model, root=case.root, gateway=case.gateway,
                                    retrieval_config={'channels': ['bm25'], 'rerank': False})
        with self.assertRaises(PlatformError) as caught:
            self.turn(case)
        self.assertEqual(caught.exception.code, 'TURN_IN_PROGRESS')
        with closing(sqlite3.connect(case.service.store.path)) as db, db:
            db.execute('UPDATE requests SET expires=0')
        with self.assertRaises(PlatformError) as caught:
            self.turn(case)
        self.assertEqual(caught.exception.code, 'TURN_INTERRUPTED')
        self.assertEqual(len(case.model.calls), 1)
        self.assertTrue(any(e['kind'] == 'graph_node_started' for e in self.events(case)))

    def test_citation_correction_still_uses_current_originals(self):
        def invalid_quote(messages):
            value = cited_answer(messages)
            value['claims'][0]['quotes'][0]['quote'] = 'not in original'
            return value
        case = self.setup_service([('decision', decision()), ('retrieval', decision('answer')),
            ('answer', invalid_quote), ('answer', cited_answer), ('verifier', verdict())])
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['trace']['citation_repair']['attempts'], 1)
        self.assertEqual(len(case.model.calls), 5)

    def test_history_restore_keeps_user_statement_provenance(self):
        case = self.setup_service()
        self.turn(case, message='你好', request='greeting')
        seq = next(e['seq'] for e in self.events(case) if e['kind'] == 'user')
        case.model.script = [('decision', decision('read_history', intent='followup', query='你好', event_seq=seq)),
            ('retrieval', decision('answer', intent='followup')),
            ('answer', {'answer': '你说了你好', 'abstain': False, 'claims': [], 'completion': 'complete',
                        'unresolved': [], 'user_statements': [{'event_seq': seq, 'quote': '你好'}]}),
            ('verifier', verdict())]
        result = self.turn(case, message='我说了什么？', request='history', revision=1)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual(result['user_statements'], [{'event_seq': seq, 'quote': '你好'}])

    def test_ambient_tracing_does_not_export_graph_state(self):
        case = self.setup_service(normal_script())
        with patch.dict('os.environ', {'LANGSMITH_TRACING': 'true', 'LANGCHAIN_TRACING_V2': 'true',
                                      'LANGSMITH_API_KEY': 'synthetic'}), \
                patch.dict('sys.modules', {'langgraph': None, 'langsmith': None}), \
                patch('httpx.Client.send', side_effect=AssertionError('Unexpected remote tracing')) as send:
            result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        send.assert_not_called()

    def test_real_sdk_unknown_receipt_still_blocks_new_graph_after_restart(self):
        case = self.setup_service()
        config = case.root / 'providers.json'
        config.write_text(json.dumps({'cache_dir': str(case.root / 'cache'), 'log_path': str(case.root / 'calls.jsonl'),
            'luna': {'api_key': 'synthetic', 'base_url': 'https://example.invalid/v1', 'model': 'synthetic',
                     'retries': 2, 'cache_mode': 'disabled'}}), encoding='utf-8')
        ledger = BudgetLedger(case.root / 'budget.sqlite3', {'model': 10, 'input_tokens': 100000,
            'output_tokens': 100000, 'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        sent = []
        def interrupted(request):
            sent.append(1)
            raise httpx.ReadTimeout('synthetic unknown outcome')
        def attach():
            provider = CapturedLuna(config)
            self.addCleanup(provider.close)
            observe_client(provider, ledger, case.root / 'capture', category='graph-contract',
                           inner=httpx.MockTransport(interrupted))
            case.service = AgentService(None, AgentModel(LunaModelProvider(provider)), root=case.root, gateway=case.gateway,
                                        retrieval_config={'channels': ['bm25'], 'rerank': False})
        attach()
        result = self.turn(case)
        self.assertEqual(result['stop_reason'], 'PROVIDER_OUTCOME_UNKNOWN', result)
        before = ledger.summary()
        self.assertEqual(before['pending_or_unknown'], 1)
        self.assertGreater(before['effective_usage_or_reserved']['output_tokens'], 0)
        attach()
        self.assertEqual(self.turn(case), result)
        retry = self.turn(case, request='new-turn', revision=1)
        self.assertEqual(retry['stop_reason'], 'RAG_PHYSICAL_REQUEST_ADMISSION_FAILED', retry)
        self.assertEqual(sent, [1])
        self.assertEqual(ledger.summary(), before)
