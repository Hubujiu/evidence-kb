import ast
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from kb_platform.agent_engine import execution_scope
from kb_platform.core import PlatformError
from kb_platform.knowledge_turn import KnowledgeTurn
from scripts.knowledge_workflow_checks import knowledge_workflow_violations
from . import agent_graph_contracts as graph
from .formal_state_machines import registry_violations, source_digest


class KnowledgeWorkflowContracts(unittest.TestCase):
    setup_service = graph.AgentGraphContracts.setup_service
    turn = graph.AgentGraphContracts.turn
    events = graph.AgentGraphContracts.events

    def test_unknown_completion_is_resolved_without_failure_publication_or_model_replay(self):
        from kb_platform.database_contracts import CommitUnknown

        case = self.setup_service(graph.normal_script())
        original = case.service.store.finish

        def lose_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise CommitUnknown('synthetic-commit-operation')

        with patch.object(case.service.store, 'finish', side_effect=lose_ack), \
                patch.object(case.service.store, 'fail', side_effect=AssertionError('unknown cannot become failed')):
            with self.assertRaises(CommitUnknown):
                self.turn(case)
        calls = len(case.model.calls)
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(len(case.model.calls), calls)
        self.assertEqual(sum(row['kind'] == 'turn_completed' for row in self.events(case)), 1)
        case.authority.rows[0]['policy']['llm_context'] = False
        with self.assertRaises(PlatformError):
            self.turn(case)
        self.assertEqual(len(case.model.calls), calls)

    def failed_tool_turn(self, *, after_evidence=False):
        script = [('decision', graph.decision())]
        if after_evidence:
            script.append(('retrieval', graph.decision(query='second private query')))
        case = self.setup_service(script, policy={'mode': 'LOCKED', 'source_ids': ['s0']})
        original = case.authority.backend.search
        calls = []

        def search(*args, **kwargs):
            calls.append(1)
            if after_evidence and len(calls) == 1:
                return original(*args, **kwargs)
            raise PlatformError('INVALID_SOURCE', 'synthetic failure', 400)

        case.authority.backend.search = search
        result = self.turn(case)
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(result['stop_reason'], 'tool_failure')
        self.assertEqual(len(calls), 2 if after_evidence else 1)
        self.assertEqual(len(case.model.calls), 2 if after_evidence else 1)
        return case, result, calls

    def test_tool_failure_response_has_no_answer_claims_or_evidence(self):
        for after_evidence in (False, True):
            with self.subTest(after_evidence=after_evidence):
                _, result, _ = self.failed_tool_turn(after_evidence=after_evidence)
                self.assertEqual(result['answer'], '')
                self.assertEqual(result['claims'], [])
                self.assertEqual(result['evidence'], [])
                self.assertEqual(result['completion'], 'error')

    def test_tool_failure_is_journaled_as_failure_not_assistant_answer(self):
        case, result, _ = self.failed_tool_turn(after_evidence=True)
        events = self.events(case)
        self.assertEqual([e['payload']['code'] for e in events if e['kind'] == 'turn_failed'],
                         ['tool_failure'])
        self.assertFalse([e for e in events if e['kind'] == 'assistant'])
        self.assertEqual([e['payload'] for e in events if e['kind'] == 'turn_completed'], [result])
        receipts = [e['payload']['status'] for e in events if e['kind'] == 'tool_result']
        self.assertEqual(receipts, ['ok', 'error'])

    def test_tool_failure_trace_redacts_derived_data_but_retains_receipts(self):
        _, result, _ = self.failed_tool_turn(after_evidence=True)
        self.assertNotIn('Original rule: value 42.', json.dumps(result))
        self.assertNotIn('second private query', json.dumps(result['trace']['tool_calls']))
        self.assertEqual([row['status'] for row in result['trace']['tool_calls']], ['ok', 'error'])
        self.assertEqual(result['trace']['tool_calls'][-1]['error'], 'INVALID_SOURCE')
        for row in result['trace']['tool_calls']:
            self.assertNotIn('args', row)
            self.assertNotIn('result', row)

    def test_failed_request_replay_preserves_redaction_without_reexecuting(self):
        case, result, calls = self.failed_tool_turn(after_evidence=True)
        replay = self.turn(case)
        self.assertEqual(replay['status'], result['status'])
        self.assertEqual(replay['stop_reason'], result['stop_reason'])
        self.assertEqual(replay['revision'], result['revision'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(case.model.calls), 2)
        self.assertEqual(replay['answer'], '')
        self.assertEqual(replay['evidence'], [])
        self.assertEqual(replay, result)

    def test_cancellation_after_tool_result_keeps_receipt_without_delivery_or_replay(self):
        cancelled = Event()
        case = self.setup_service([('decision', graph.decision())])
        original = case.authority.backend.search

        def search(*args, **kwargs):
            result = original(*args, **kwargs)
            cancelled.set()
            return result

        case.authority.backend.search = search
        with patch('kb_platform.agent_engine.time.monotonic', return_value=100.0), \
                patch('kb_platform.knowledge_turn.time.perf_counter', return_value=100.0):
            with execution_scope(deadline_seconds=30, cancellation=cancelled):
                result = self.turn(case)
        self.assertEqual(result['stop_reason'], 'AGENT_CANCELLED')
        self.assertEqual(result['status'], 'error')
        self.assertEqual((result['answer'], result['claims'], result['evidence']), ('', [], []))
        self.assertEqual(len(case.model.calls), 1)
        receipts = [e for e in self.events(case) if e['kind'] == 'tool_result']
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['payload']['status'], 'ok')
        self.assertNotIn('compose', result['trace']['graph']['nodes'])
        self.assertEqual(self.turn(case), result)
        self.assertEqual(len(case.model.calls), 1)

    def test_authority_change_after_graph_completion_is_refused_before_commit(self):
        case = self.setup_service(graph.normal_script())
        original = case.service.runtime.run

        def run(*args, **kwargs):
            response = original(*args, **kwargs)
            self.assertEqual(response['status'], 'answered')
            case.authority.revision += 1
            return response

        with patch.object(case.service.runtime, 'run', run):
            result = self.turn(case)
        self.assertEqual(result['stop_reason'], 'SCOPE_CHANGED')
        self.assertEqual((result['answer'], result['claims'], result['evidence']), ('', [], []))
        self.assertEqual(result['trace']['graph']['nodes'][-1], 'deliver')
        self.assertFalse([e for e in self.events(case) if e['kind'] == 'assistant'])
        self.assertEqual(len([e for e in self.events(case) if e['kind'] == 'turn_failed']), 1)

    def test_result_guard_removal_reproduces_the_actual_response_failures(self):
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / 'kb_platform/knowledge_turn.py').read_text('utf-8'))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'run')
        protected = next(node for node in method.body if isinstance(node, ast.Try))
        guard = next(node for node in protected.body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == "response['status'] == 'error'")
        protected.body.remove(guard)
        namespace = {'__name__': 'kb_platform._knowledge_workflow_mutant', '__package__': 'kb_platform'}
        exec(compile(ast.fix_missing_locations(tree), '<knowledge-workflow-mutant>', 'exec'), namespace)
        names = ('test_tool_failure_response_has_no_answer_claims_or_evidence',
                 'test_tool_failure_is_journaled_as_failure_not_assistant_answer',
                 'test_tool_failure_trace_redacts_derived_data_but_retains_receipts',
                 'test_failed_request_replay_preserves_redaction_without_reexecuting')
        result = unittest.TestResult()
        with patch.object(KnowledgeTurn, 'run', namespace['KnowledgeTurn'].run):
            unittest.TestSuite(type(self)(name) for name in names).run(result)
        self.assertEqual(result.testsRun, 4)
        self.assertEqual(len(result.failures), 5)
        self.assertEqual(result.errors, [])

    def test_mandatory_result_lint_rejects_mutations_after_source_hash_refresh(self):
        root = Path(__file__).resolve().parents[2]
        relative = 'kb_platform/knowledge_turn.py'
        source = (root / relative).read_text('utf-8')
        self.assertEqual(knowledge_workflow_violations(source), [])
        self.assertEqual(knowledge_workflow_violations(ast.unparse(ast.parse(source))), [])
        with TemporaryDirectory() as directory:
            mutant_root = Path(directory)
            (mutant_root / 'kb_platform').mkdir()
            (mutant_root / 'formal').mkdir()
            machine = next(row for row in json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))[
                'machines'] if row['id'] == 'knowledge_workflow')
            machine = {**machine, 'status': 'unmodeled', 'owners': [relative], 'models': [], 'negative_controls': [],
                       'source_bindings': [{'path': relative, 'language': 'python', 'symbol': '<module>'}]}
            (mutant_root / relative).write_text(source, 'utf-8')
            machine['source_bindings'][0]['sha256'] = source_digest(mutant_root, machine['source_bindings'][0])
            positive = {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
                        'machines': [deepcopy(machine)]}
            self.assertEqual(registry_violations(mutant_root, positive), [])
            for mutation in ('removed', 'inverted', 'all_nonanswered', 'late', 'swallowed', 'wrong_reason',
                             'replace_response', 'leak_failure_answer', 'success_handler', 'double_failure'):
                with self.subTest(mutation=mutation):
                    tree = ast.parse(source)
                    method = next(node for node in ast.walk(tree)
                                  if isinstance(node, ast.FunctionDef) and node.name == 'run')
                    protected = next(node for node in method.body if isinstance(node, ast.Try))
                    guard = next(node for node in protected.body if isinstance(node, ast.If)
                                 and ast.unparse(node.test) == "response['status'] == 'error'")
                    index = protected.body.index(guard)
                    if mutation == 'removed':
                        protected.body.remove(guard)
                    elif mutation == 'inverted':
                        guard.test.ops = [ast.NotEq()]
                    elif mutation == 'all_nonanswered':
                        guard.test = ast.parse("response['status'] != 'answered'", mode='eval').body
                    elif mutation == 'late':
                        protected.body.remove(guard)
                        protected.body.insert(-1, guard)
                    elif mutation == 'swallowed':
                        protected.body[index] = ast.Try(body=[guard], handlers=[ast.ExceptHandler(
                            type=ast.Name(id='Exception', ctx=ast.Load()), name=None, body=[ast.Pass()])],
                            orelse=[], finalbody=[])
                    elif mutation == 'wrong_reason':
                        guard.body[0].exc.args[0] = ast.Constant('OTHER_FAILURE')
                    elif mutation == 'replace_response':
                        protected.body.insert(index + 1, ast.parse("response = {'status': 'answered'}").body[0])
                    elif mutation == 'leak_failure_answer':
                        failure = protected.handlers[0].body[-1].value.args[-1]
                        offset = next(i for i, key in enumerate(failure.keys) if key.value == 'answer')
                        failure.values[offset] = ast.parse("response['answer']", mode='eval').body
                    elif mutation == 'success_handler':
                        protected.handlers[0].body[-1] = deepcopy(protected.body[-1])
                    else:
                        protected.handlers[0].body.insert(-1, ast.Expr(
                            value=deepcopy(protected.handlers[0].body[-1].value)))
                    changed = ast.unparse(ast.fix_missing_locations(tree))
                    (mutant_root / relative).write_text(changed, 'utf-8')
                    registry = {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
                                'machines': [deepcopy(machine)]}
                    for machine in registry['machines']:
                        for binding in machine['source_bindings']:
                            if binding['path'] == relative:
                                binding['sha256'] = source_digest(mutant_root, binding)
                    errors = registry_violations(mutant_root, registry)
                    self.assertFalse([e for e in errors if 'source review drift' in e], errors)
                    self.assertTrue(any(e.startswith('knowledge_workflow: knowledge workflow') for e in errors), errors)
