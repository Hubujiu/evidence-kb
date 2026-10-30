from types import SimpleNamespace
import unittest

from . import agent_graph_contracts as fixtures
from .product_fixture import Reranker
from kb_platform.model_configuration import TextView
from kb_platform.retrieval_pipeline import LunaCandidateReranker


class RerankerReviewContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

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
