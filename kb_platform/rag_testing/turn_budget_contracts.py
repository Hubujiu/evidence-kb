from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import unittest

import httpx

from kb_platform.agent_model import AgentModel, BudgetLedger
from kb_platform.agent_luna import LunaModelProvider
from kb_platform.model_configuration import TextView
from kb_platform.model_errors import RemoteModelError
from kb_platform.retrieval_pipeline import LunaCandidateReranker
from types import SimpleNamespace
from . import agent_graph_contracts as fixtures
from .budget import BudgetLedger as PhysicalLedger
from .provider_capture import CapturedLuna, observe_client
from .harness_context_model_contracts import _Provider


class TurnBudgetContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

    def options(self, turn='turn', limit=20):
        return {'role': 'answer', 'schema': {'type': 'object'},
                'call_context': {'session_id': 'session', 'turn_id': turn, 'max_physical_calls': limit}}

    def test_models_share_physical_attempt_limit_and_keep_turns_independent(self):
        ledger = BudgetLedger(max_calls=100)
        generation = AgentModel(_Provider([{}] * 21), ledger=ledger)
        reranker = AgentModel(_Provider([{}] * 2), ledger=ledger)
        messages = [{'role': 'user', 'content': 'original'}]
        reranker.complete(messages, **self.options())
        for _ in range(19):
            generation.complete(messages, **self.options())
        with self.assertRaisesRegex(RemoteModelError, 'AGENT_CALL_BUDGET_EXHAUSTED'):
            generation.complete(messages, **self.options())
        self.assertEqual(ledger.snapshot()['calls'], 20)
        self.assertEqual(generation.turn_budget_snapshot('session', 'turn')['calls'], 20)
        self.assertEqual(reranker.turn_budget_snapshot('session', 'turn')['calls'], 20)
        generation.complete(messages, **self.options(turn='next'))
        self.assertEqual(generation.turn_budget_snapshot('session', 'next')['calls'], 1)

    def test_cross_model_pending_reservation_blocks_other_model_before_send(self):
        entered, finish = Event(), Event()

        def blocked():
            entered.set()
            if not finish.wait(3):
                raise RuntimeError('fixture gate timed out')

        ledger = BudgetLedger(max_calls=100)
        provider = _Provider([{}], after=blocked)
        generation = AgentModel(provider, ledger=ledger)
        reranker = AgentModel(_Provider([{}]), ledger=ledger)
        messages = [{'role': 'user', 'content': 'original'}]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(generation.complete, messages, **self.options(limit=1))
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(RemoteModelError, 'AGENT_CALL_BUDGET_EXHAUSTED'):
                    reranker.complete(messages, **self.options(limit=1))
            finally:
                finish.set()
            future.result()
        self.assertEqual(len(reranker.provider.calls), 0)
        self.assertEqual(ledger.snapshot()['calls'], 1)

    def test_known_unsent_releases_shared_reservation_and_conflicting_limit_is_rejected(self):
        class Unsent(_Provider):
            def complete(self, messages, output_limit, *, purpose, key, trace):
                raise RemoteModelError('SYNTHETIC_UNSENT')

        ledger = BudgetLedger(max_calls=100)
        generation = AgentModel(Unsent([]), ledger=ledger)
        reranker = AgentModel(_Provider([{}]), ledger=ledger)
        messages = [{'role': 'user', 'content': 'original'}]
        with self.assertRaisesRegex(RemoteModelError, 'SYNTHETIC_UNSENT'):
            generation.complete(messages, **self.options(limit=1))
        self.assertEqual(reranker.complete(messages, **self.options(limit=1)).attempts, 1)
        self.assertEqual(ledger.snapshot()['calls'], 1)
        with self.assertRaisesRegex(RemoteModelError, 'AGENT_TURN_BUDGET_CHANGED'):
            reranker.complete(messages, **self.options(limit=2))

    def test_actual_turn_retry_rerank_and_format_repair_cannot_send_twenty_first_request(self):
        case = self.setup_service(config={'novelty_search': False})
        script = [fixtures.decision('search_pipeline')]
        script.extend(fixtures.decision('search', query=f'follow-up {index}') for index in range(5))
        script.extend([fixtures.decision('answer'), '{', fixtures.cited_answer, fixtures.verdict()])
        physical = PhysicalLedger(case.root / 'physical.sqlite', {
            'model': 100, 'input_tokens': 5_000_000, 'output_tokens': 500_000,
            'remote_embedding_calls': 0, 'embedding_input_tokens': 0})
        budget = BudgetLedger(max_calls=100, max_tokens=5_000_000)
        sent = {'generation': 0, 'rerank': 0}

        def make_model(name, retries):
            config = case.root / f'{name}.json'
            config.write_text(json.dumps({'cache_dir': str(case.root / name),
                'log_path': str(case.root / f'{name}.jsonl'), 'luna': {
                    'api_key': 'synthetic', 'base_url': 'https://fixture.invalid/v1',
                    'model': name, 'retries': retries, 'cache_mode': 'disabled'}}), 'utf-8')
            client = CapturedLuna(config)
            self.addCleanup(client.close)

            def respond(request):
                sent[name] += 1
                if retries and sent[name] % 2:
                    return httpx.Response(503, json={'error': 'synthetic retry'})
                messages = json.loads(request.content)['messages']
                value = script.pop(0) if name == 'generation' else {
                    'scores': [0.2, 0.9], 'requirements': [
                        {'need': 'original', 'supported': False, 'evidence': []}]}
                value = value(messages) if callable(value) else value
                return httpx.Response(200, json={'choices': [{'message': {
                    'content': value if isinstance(value, str) else json.dumps(value)},
                    'finish_reason': 'stop'}], 'usage': {
                        'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})

            observe_client(client, physical, case.root / 'capture' / name,
                           category='turn-budget-contract', inner=httpx.MockTransport(respond))
            return AgentModel(LunaModelProvider(client), policy=case.service.context_policy, ledger=budget)

        generation, reranker = make_model('generation', 1), make_model('rerank', 0)
        case.service.model = generation
        models = SimpleNamespace(catalog=lambda: {'generation': SimpleNamespace(
            provider='magpie_luna', model='rerank', revision=1)})
        case.gateway.pipeline_reranker = LunaCandidateReranker(TextView(models, reranker))
        case.service.retrieval_config = {'channels': ['bm25'], 'rerank': True, 'parent': 'none'}
        result = self.turn(case)
        self.assertEqual(result['stop_reason'], 'AGENT_CALL_BUDGET_EXHAUSTED', sent)
        self.assertEqual(sent, {'generation': 18, 'rerank': 1})
        self.assertEqual(len(result['trace']['format_repairs']), 1)
        self.assertEqual(physical.summary()['physical_requests'], 19)
        self.assertEqual(budget.snapshot()['calls'], 19)
        self.assertEqual(generation.turn_budget_snapshot(case.session, 'turn'),
                         reranker.turn_budget_snapshot(case.session, 'turn'))
        self.assertEqual(len(script), 1)


if __name__ == '__main__':
    unittest.main()
