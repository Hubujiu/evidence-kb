from types import SimpleNamespace
from copy import deepcopy
import json
import unittest

from . import agent_graph_contracts as fixtures
from .product_fixture import Reranker
from kb_platform.model_configuration import TextView
from kb_platform.retrieval_pipeline import LunaCandidateReranker
from kb_platform.context_budget import estimate_messages
from .product_fixture import Authority


class RerankerReviewContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

    def test_large_candidate_turn_preserves_all_positions_within_final_request_budget(self):
        case = self.setup_service([
            ('decision', fixtures.decision('search_pipeline')),
            ('retrieval', fixtures.decision('answer')),
            ('answer', fixtures.cited_answer), ('verifier', fixtures.verdict())])
        case.authority.__dict__.update(Authority([
            (f'Original-{index}.md', f'Original {index}: ' + '中文原文内容' * 108)
            for index in range(20)]).__dict__)
        before = deepcopy(case.authority.records)

        def ranking(messages):
            payload = json.loads(messages[-1]['content'])
            self.assertEqual(payload['query'], 'original')
            self.assertEqual([row['ref'] for row in payload['documents']],
                             [f'R{index:03}' for index in range(1, 21)])
            for index, row in enumerate(payload['documents']):
                self.assertTrue(row['text'].startswith(f'Original {index}: '))
                self.assertTrue(row['text_truncated'])
                self.assertLess(len(row['text']), 650)
            self.assertLessEqual(estimate_messages(messages)['tokens'], 24000)
            return {'scores': [index / 20 for index in range(20)], 'requirements': [
                {'need': 'original rule', 'supported': True,
                 'evidence': [{'ref': 'R020', 'quote': 'Original 19:'}]}]}

        rerank_model = fixtures.ScenarioModel([('rerank', ranking)])
        models = SimpleNamespace(catalog=lambda: {'generation': SimpleNamespace(
            provider='magpie_luna', model='synthetic-rerank', revision=1)})
        case.gateway.pipeline_reranker = LunaCandidateReranker(TextView(models, rerank_model))
        case.service.retrieval_config = {'channels': ['bm25'], 'candidate_limit': 20,
            'fused_limit': 20, 'top_k': 1, 'parent': 'none', 'rerank': True}
        case.service.capture = case.captures.append
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result.get('stop_reason'))
        self.assertEqual(len(rerank_model.calls), 1)
        capture = next(row for row in case.captures if row['role'] == 'rerank')
        self.assertEqual(len(capture['prepared'].projection['evidence']), 20)
        self.assertEqual(case.authority.records, before)

    def test_actual_turn_uses_injected_vendor_reranker_and_hybrid_search(self):
        case = self.setup_service([
            ('decision', fixtures.decision('search_pipeline')),
            ('retrieval', fixtures.decision('answer')),
            ('answer', fixtures.cited_answer), ('verifier', fixtures.verdict())])
        selected = Reranker()
        selected.provider_id = 'vendor-a'
        selected.manifest = {'provider': 'vendor-a', 'model': 'local-contract'}
        case.gateway.provider_id = 'vendor-a'
        case.gateway.pipeline_reranker = selected
        for row in case.authority.rows:
            row['policy']['allowed_providers'] = ['vendor-a', 'google_gemini']
        case.service.retrieval_config = {
            'channels': ['bm25', 'dense'], 'rerank': True, 'parent': 'none', 'top_k': 2}
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertTrue(result['trace']['pipeline_capabilities']['rerank_all_sources_admitted'])
        self.assertTrue(selected.inputs)
        self.assertIs(case.gateway.pipeline_reranker, selected)
        self.assertEqual({channel for channel, _ in case.authority.calls}, {'bm25', 'dense'})
        self.assertEqual([call['role'] for call in case.model.calls],
                         ['decision', 'retrieval', 'answer', 'verifier'])

    def test_disabled_reranker_stays_disabled_during_actual_turn_with_legacy_client(self):
        case = self.setup_service([
            ('decision', fixtures.decision('search_pipeline')),
            ('retrieval', fixtures.decision('answer')),
            ('answer', fixtures.cited_answer), ('verifier', fixtures.verdict())])

        def forbidden(*args, **kwargs):
            raise AssertionError('disabled reranker must not call the legacy client')

        case.gateway.research = SimpleNamespace(luna=SimpleNamespace(
            provider_id='magpie_luna', complete_json=forbidden),
            _directory=lambda actor, scope: case.root)
        case.gateway.pipeline_reranker = None
        case.service.retrieval_config = {'channels': ['bm25'], 'rerank': False, 'parent': 'none'}
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        capabilities = result['trace']['pipeline_capabilities']
        self.assertFalse(capabilities['reranker_configured'])
        self.assertFalse(capabilities['rerank_all_sources_admitted'])
        self.assertEqual(capabilities['rerank_eligible_source_count'], 0)
        self.assertIsNone(case.gateway.pipeline_reranker)

    def test_turn_accounts_actual_selected_model_without_replacing_rank_algorithm(self):
        case = self.setup_service([
            ('decision', fixtures.decision('search_pipeline')),
            ('retrieval', fixtures.decision('answer')),
            ('answer', fixtures.cited_answer), ('verifier', fixtures.verdict())])

        def ranking(messages):
            self.assertIn('coverage-array/1', messages[0]['content'])
            self.assertIn('documents', messages[-1]['content'])
            return {'scores': [0.2, 0.9], 'requirements': [
                {'need': 'original rule', 'supported': False, 'evidence': []}]}

        rerank_model = fixtures.ScenarioModel([('rerank', ranking)])
        models = SimpleNamespace(catalog=lambda: {'reranking': SimpleNamespace(
            provider='vendor-a', model='rerank-local', revision=1)})
        selected = LunaCandidateReranker(TextView(models, rerank_model, 'reranking'))
        case.gateway.provider_id = 'vendor-a'
        case.gateway.pipeline_reranker = selected
        for row in case.authority.rows:
            row['policy']['allowed_providers'] = ['vendor-a', 'google_gemini']
        case.service.retrieval_config = {'channels': ['bm25'], 'rerank': True, 'parent': 'none'}
        case.service.capture = case.captures.append
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        self.assertEqual([call['role'] for call in case.model.calls],
                         ['decision', 'retrieval', 'answer', 'verifier'])
        self.assertEqual([call['role'] for call in rerank_model.calls], ['rerank'])
        self.assertEqual(sum(call['role'] == 'rerank' for call in result['trace']['calls']), 1)
        self.assertEqual(sum(call['role'] == 'rerank' for call in case.captures), 1)
        self.assertIs(case.gateway.pipeline_reranker, selected)


if __name__ == '__main__':
    unittest.main()
