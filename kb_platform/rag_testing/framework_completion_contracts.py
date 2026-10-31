from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from kb_platform.agent_context import ContextManager, ContextPolicy, estimate_messages
from kb_platform.agent_luna import LunaModelProvider
from kb_platform.agent_model import AgentModel
from kb_platform.agent_protocol import ANSWER_SCHEMA, DECISION_SCHEMA, VERDICT_SCHEMA
from kb_platform.agent_schema import compile_schema, validate_output
from kb_platform.agent_tool_registry import TOOLS, advanced_tools, bind_decision_evidence, decision_schema, tool_arguments
from kb_platform.provider_runtime import ToolExecutor, RuntimePolicyError


def status_error(status, retry_after=None):
    response = httpx.Response(status, request=httpx.Request('GET', 'https://example.invalid'),
                              headers={} if retry_after is None else {'Retry-After': retry_after})
    return httpx.HTTPStatusError('synthetic', request=response.request, response=response)


class TypedProtocolContracts(unittest.TestCase):
    def validate(self, value, schema):
        validate_output(value, compile_schema(schema))

    def test_source_dependent_actions_require_discovered_evidence(self):
        original = decision_schema({'explicit_completion': True}, {'workflow_next', 'table_join'}, False)
        empty = bind_decision_evidence(original, [])
        available = bind_decision_evidence(original, ['known'])
        for action in ('read', 'workflow_next', 'table_join'):
            value = {'intent': 'knowledge', 'action': action}
            for schema, references in ((empty, []), (empty, ['invented']),
                                       (available, []), (available, ['invented'])):
                with self.subTest(action=action, references=references), self.assertRaises(ValueError):
                    self.validate({**value, 'citation_ids': references}, schema)
        self.validate({'intent': 'knowledge', 'action': 'workflow_next', 'citation_ids': ['known']}, available)
        self.validate({'intent': 'knowledge', 'action': 'search', 'query': 'workflow'}, empty)
        self.assertIn('workflow_next', original['properties']['action']['enum'])
        revoked = bind_decision_evidence(original, [])
        with self.assertRaises(ValueError):
            self.validate({'intent': 'knowledge', 'action': 'workflow_next', 'citation_ids': ['known']}, revoked)

    def test_optional_fields_are_absent_not_defaulted_or_nullable(self):
        value = {'intent': 'knowledge', 'action': 'answer'}
        self.validate(value, DECISION_SCHEMA)
        self.assertEqual(value, {'intent': 'knowledge', 'action': 'answer'})
        for field in ('query', 'offset', 'event_seq', 'topic_changed', 'citation_ids'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate({**value, field: None}, DECISION_SCHEMA)

    def test_strict_types_bounds_and_extra_fields(self):
        for fields in ({'offset': True}, {'offset': 1.0}, {'offset': -1}, {'offset': 1000001},
                       {'event_seq': 0}, {'topic_changed': 1}, {'query': 'x' * 6001},
                       {'citation_ids': ['x'] * 11}, {'subquestions': ['x'] * 7}, {'unknown': 1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.validate({'intent': 'knowledge', 'action': 'search', **fields}, DECISION_SCHEMA)

    def test_nested_answer_contract_and_no_external_references(self):
        answer = {'answer': 'a', 'abstain': False, 'claims': [
            {'text': 'a', 'evidence_ids': ['c'], 'quotes': [{'evidence_id': 'c', 'quote': 'a'}]}]}
        self.validate(answer, ANSWER_SCHEMA)
        for change in ('extra', 'empty_quotes', 'empty_ids'):
            bad = deepcopy(answer)
            if change == 'extra':
                bad['claims'][0]['quotes'][0]['forged'] = True
            else:
                bad['claims'][0]['quotes' if change == 'empty_quotes' else 'evidence_ids'] = []
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(bad, ANSWER_SCHEMA)
        self.validate({'supported': True, 'reason': ''}, VERDICT_SCHEMA)
        for schema in (DECISION_SCHEMA, ANSWER_SCHEMA, VERDICT_SCHEMA):
            self.assertNotIn('$ref', repr(schema))
            self.assertNotIn('$defs', repr(schema))

    def test_registry_matches_planner_actions_and_feature_gates(self):
        base = decision_schema({}, set(), False)
        self.assertEqual(set(base['properties']['action']['enum']),
                         {'search', 'read', 'read_history', 'calculator', 'answer', 'clarify'})
        gateway = SimpleNamespace(table=Mock(), search_pipeline=Mock())
        advanced = advanced_tools({'advanced_tools': True}, gateway)
        self.assertEqual(advanced, {'table', 'search_pipeline'})
        self.assertEqual(advanced_tools({}, gateway), set())
        enabled = decision_schema({'page_navigation': True}, advanced, True)
        self.assertEqual(set(enabled['properties']['action']['enum']),
                         set(base['properties']['action']['enum']) | advanced | {'search_pages', 'search_semantic_pages'})
        full = decision_schema({'explicit_completion': True, 'page_navigation': True},
                               {n for n, s in TOOLS.items() if s.feature == 'advanced_tools'}, True)
        self.assertEqual(set(full['properties']['action']['enum']) - {'answer', 'clarify'}, set(TOOLS))
        self.assertEqual(decision_schema({}, set(), False), base)

    def test_advanced_schema_preserves_scalar_and_nullable_contracts(self):
        schema = decision_schema({'explicit_completion': True}, {'table'}, False)
        for scalar in ('s', 3, 3.5, True, None):
            self.validate({'intent': 'knowledge', 'action': 'table', 'column': None,
                           'filters': [{'column': 'x', 'op': 'eq', 'value': scalar}]}, schema)
        for scalar in ([], {}, float('nan'), float('inf')):
            with self.subTest(scalar=scalar), self.assertRaises(ValueError):
                self.validate({'intent': 'knowledge', 'action': 'table',
                               'filters': [{'column': 'x', 'op': 'eq', 'value': scalar}]}, schema)

    def test_argument_projection_preserves_legacy_defaults_and_excludes_other_fields(self):
        config = {'rewrite': True, 'expand_history': True}
        self.assertEqual(tool_arguments('search', {'query': 'rewrite', 'secret': 42}, 'original', config),
                         {'query': 'rewrite'})
        self.assertEqual(tool_arguments('search', {'query': 'rewrite'}, 'original', {'rewrite': False}),
                         {'query': 'original'})
        self.assertEqual(tool_arguments('read', {}, '', config), {'citation_ids': [], 'offset': 0})
        self.assertEqual(tool_arguments('read_history', {'query': 'q'}, '', config),
                         {'query': 'q', 'offset': 0, 'event_seq': None})
        self.assertEqual(tool_arguments('table', {'citation_ids': ['c']}, '', config),
                         {'citation_ids': ['c'], 'operation': 'rows', 'column': None, 'filters': []})
        self.assertEqual(tool_arguments('table_join', {'left_key': 'a', 'right_key': 'b'}, '', config),
                         {'citation_ids': [], 'left_key': 'a', 'right_key': 'b', 'how': 'inner'})
        self.assertEqual(tool_arguments('calculator', {}, '', config), {'expression': ''})
        self.assertEqual(tool_arguments('workflow_next', {'citation_ids': ['c']}, '', config), {'citation_ids': ['c']})
        with self.assertRaises(KeyError):
            tool_arguments('unknown', {}, '', config)

    def test_provider_backed_tools_have_no_outer_retry(self):
        self.assertFalse(TOOLS['search_pipeline'].retry_safe)
        self.assertFalse(TOOLS['search_semantic_pages'].retry_safe)
        self.assertTrue(TOOLS['read'].retry_safe)
        self.assertTrue(TOOLS['search_pipeline'].incurs_cost)
        self.assertFalse(TOOLS['search_pipeline'].mutates_data)


class ToolRetryContracts(unittest.TestCase):
    def execute(self, function, *, read_only=True, max_attempts=3, max_wait=2, deadline=None, sleep=None):
        executor = ToolExecutor(max_attempts=max_attempts, max_wait_seconds=max_wait, sleep=sleep or Mock())
        result = executor.execute('request', 'tool', {'q': 'original'}, function,
                                   read_only=read_only, deadline_seconds=deadline)
        return executor, result

    def test_confirmed_transient_retries_and_stable_receipt(self):
        function, sleep = Mock(side_effect=[httpx.ConnectError('offline'), status_error(503), ['value']]), Mock()
        executor, result = self.execute(function, sleep=sleep)
        self.assertEqual((result.status, result.value, result.attempts), ('COMPLETE', ['value'], 3))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [.25, .5])
        result.value.append('mutated')
        reused = executor.execute('request', 'tool', {'q': 'original'}, function, read_only=True)
        self.assertTrue(reused.reused)
        self.assertEqual(reused.value, ['value'])
        self.assertEqual(function.call_count, 3)
        with self.assertRaisesRegex(RuntimePolicyError, 'TOOL_REQUEST_ID_REUSED'):
            executor.execute('request', 'tool', {'q': 'changed'}, function)

    def test_non_readonly_unknown_and_permanent_errors_never_retry(self):
        for error, readonly, expected in [(httpx.ConnectError('x'), False, 'ERROR'),
                                          (httpx.ReadTimeout('x'), True, 'UNKNOWN'),
                                          (httpx.WriteTimeout('x'), True, 'UNKNOWN'),
                                          (status_error(403), True, 'ERROR'),
                                          (RuntimePolicyError('PROVIDER_REQUEST_UNRESOLVED'), True, 'ERROR')]:
            with self.subTest(error=error, readonly=readonly):
                function = Mock(side_effect=error)
                executor, result = self.execute(function, read_only=readonly)
                self.assertEqual(result.status, expected)
                self.assertEqual(function.call_count, 1)
                self.assertTrue(executor.execute('request', 'tool', {'q': 'original'}, function).reused)
                self.assertEqual(function.call_count, 1)

    def test_retry_after_and_cumulative_wait_cap(self):
        function, sleep = Mock(side_effect=status_error(429, '0.6')), Mock()
        _, result = self.execute(function, max_wait=1.0, sleep=sleep)
        self.assertEqual((result.status, result.attempts), ('ERROR', 2))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [.6])

    def test_large_retry_after_is_not_slept_or_clamped(self):
        function, sleep = Mock(side_effect=status_error(429, '100')), Mock()
        _, result = self.execute(function, sleep=sleep)
        self.assertEqual(result.attempts, 1)
        sleep.assert_not_called()

    def test_empty_is_success_and_never_retried(self):
        function = Mock(return_value=[])
        _, result = self.execute(function)
        self.assertEqual((result.status, result.attempts), ('EMPTY', 1))
        self.assertEqual(function.call_count, 1)

    def test_initial_deadline_and_retry_deadline(self):
        with patch('kb_platform.provider_runtime.time.monotonic', return_value=100):
            function = Mock()
            _, result = self.execute(function, deadline=0)
            self.assertEqual((result.error_code, result.attempts), ('TOOL_DEADLINE_EXCEEDED', 0))
            function.assert_not_called()
            function, sleep = Mock(side_effect=httpx.ConnectError('x')), Mock()
            _, result = self.execute(function, deadline=.25, sleep=sleep)
            self.assertEqual(result.attempts, 1)
            sleep.assert_not_called()

    def test_late_result_is_not_delivered(self):
        now = [100]
        def function():
            now[0] += 2
            return 'too late'
        with patch('kb_platform.provider_runtime.time.monotonic', side_effect=lambda: now[0]):
            _, result = self.execute(function, deadline=1)
        self.assertEqual((result.status, result.category, result.value), ('ERROR', 'late_result', None))

    def test_cancelled_call_or_sleep_is_retained_as_unknown(self):
        for during_sleep in (False, True):
            with self.subTest(during_sleep=during_sleep):
                function = Mock(side_effect=httpx.ConnectError('x') if during_sleep else KeyboardInterrupt())
                executor = ToolExecutor(sleep=Mock(side_effect=RuntimeError('interrupted sleep')))
                with self.assertRaises((KeyboardInterrupt, RuntimeError)):
                    executor.execute('id', 'tool', {}, function, read_only=True)
                result = executor.execute('id', 'tool', {}, function, read_only=True)
                self.assertEqual((result.status, result.error_code), ('UNKNOWN', 'TOOL_EXECUTION_INTERRUPTED'))
                self.assertEqual(function.call_count, 1)


class TokenCapabilityContracts(unittest.TestCase):
    def test_estimate_never_claims_exact_or_verified(self):
        result = estimate_messages([{'role': 'user', 'content': '中文😀'}])
        self.assertFalse(result['exact'])
        self.assertFalse(result['capability_verified'])
        self.assertEqual(result['kind'], 'conservative_utf8_estimate')

    def test_strict_context_rejects_before_projection(self):
        context = ContextManager(ContextPolicy(strict_tokens=True))
        with self.assertRaisesRegex(ValueError, 'TOKEN_CAPABILITY_UNVERIFIED'):
            context.prepare('answer', 'system', {}, [], [], 'question')

    def test_strict_model_rejects_before_reservation_and_transport(self):
        luna, ledger = Mock(), Mock()
        model = AgentModel(LunaModelProvider(luna), ContextPolicy(strict_tokens=True), ledger)
        with self.assertRaisesRegex(Exception, 'TOKEN_CAPABILITY_UNVERIFIED'):
            model.complete([], role='answer', schema={}, call_context={})
        ledger.reserve.assert_not_called()
        luna._post.assert_not_called()
