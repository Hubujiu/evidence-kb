from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.context_budget import RequestBudget
from kb_platform.knowledge_contracts import EvidenceClient, EvidenceEndpoint, EVIDENCE_METHODS
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.retrieval_pipeline import LunaCandidateReranker
from kb_platform.retrieval_assessment import unpack_assessment
from kb_platform.knowledge_turn import KnowledgeTurn
from .product_fixture import Authority, Gateway, Reranker
from . import agent_graph_contracts as fixtures


class EvidenceBoundaryContracts(unittest.TestCase):
    setup_service = fixtures.AgentGraphContracts.setup_service
    turn = fixtures.AgentGraphContracts.turn

    def test_long_early_paragraph_cannot_starve_short_facts_in_either_order(self):
        manager = ContextManager(ContextPolicy(input_budget=24000, evidence_strategy='query_blocks'))
        items = [{'citation_id': 'noise', 'source_id': 'n', 'text': 'common ' * 700}] + [
            {'citation_id': str(i), 'source_id': str(i), 'text': f'common fact {i}: value {i}. ' * 3}
            for i in range(4)]
        original = deepcopy(items)
        budget = {'limit': 2300, 'unit': 'unicode_codepoints', 'scope': 'evidence'}
        for order in (items, list(reversed(items))):
            result = manager.prepare('answer', 'Use originals', {}, [], order, 'common fact', request_budget=budget)
            RequestBudget.parse(budget).validate_messages(result.messages)
            selected = {row['citation_id']: row.get('text') for row in result.projection['evidence']}
            for row in items[1:]:
                self.assertEqual(selected[row['citation_id']], row['text'])
        self.assertEqual(items, original)

    def test_unused_shares_can_complete_whole_paragraph_after_all_items_get_a_turn(self):
        manager = ContextManager(ContextPolicy(input_budget=24000, evidence_strategy='query_blocks'))
        items = [{'citation_id': 'long', 'source_id': 'a', 'text': 'common fact. ' * 70},
                 {'citation_id': 'short', 'source_id': 'b', 'text': 'common fact.'}]
        result = manager.prepare('answer', '', {}, [], items, 'common',
            request_budget={'limit': 1600, 'unit': 'unicode_codepoints', 'scope': 'evidence'})
        self.assertEqual([r['text'] for r in result.projection['evidence']], [r['text'] for r in items])

    def test_surplus_expansion_cannot_consume_restored_history_share(self):
        history = 'historical fact. ' * 50
        state = {'tool_results': [{'name': 'read_history', 'status': 'ok',
            'args': {'query': 'historical'}, 'result': {'events': [
                {'seq': 1, 'text': history, 'offset': 0, 'truncated': False}]}}]}
        result = ContextManager(ContextPolicy(input_budget=12000, evidence_strategy='query_blocks')).prepare(
            'answer', '', state, [], [{'citation_id': 'a', 'source_id': 'a', 'text': 'common ' * 150}],
            'common', request_budget={'limit': 2200, 'unit': 'unicode_codepoints', 'scope': 'messages'})
        self.assertEqual(result.projection['state']['tool_results'][0]['result']['events'][0]['text'], history)
        self.assertLessEqual(RequestBudget(2200).measure_messages(result.messages), 2200)

    def test_generic_context_does_not_require_knowledge_citation_identity(self):
        items = [{'document_id': 'long', 'text': 'common fact. ' * 70},
                 {'document_id': 'short', 'text': 'common fact.'}]
        result = ContextManager(ContextPolicy(input_budget=24000, evidence_strategy='query_blocks')).prepare(
            'answer', '', {}, [], items, 'common',
            request_budget={'limit': 1600, 'unit': 'unicode_codepoints', 'scope': 'evidence'})
        self.assertEqual([row['text'] for row in result.projection['evidence']], [row['text'] for row in items])

    def test_partial_contribution_is_delivered_without_claiming_complete_relation(self):
        authority = Authority([('Researcher.md', 'Mira Vale was born in 1972.'),
                               ('Namesake.md', 'Mira Vale is the name of a cargo ship.')])
        class Model:
            provider_id = 'magpie_luna'
            manifest = {'model': 'synthetic-partial-contribution'}
            def complete_json(self, system, payload, **kwargs):
                documents = json.loads(payload)['documents']
                selected = next(row['ref'] for row in documents if '1972' in row['text'])
                return {'scores': [0.2 if '1972' in row['text'] else 0.99 for row in documents],
                    'requirements': [{'need': 'birth date of the director of Unseen Film',
                                      'supported': False, 'evidence': [{'ref': selected}]}]}
        for protocol, expected_count in [('coverage-contributions/3', 1), ('coverage-references/2', 0)]:
            with self.subTest(protocol=protocol):
                ranker = LunaCandidateReranker(Model(), protocol=protocol)
                gateway = Gateway(authority, pipeline_reranker=ranker)
                endpoint = EvidenceEndpoint(gateway)
                self.addCleanup(endpoint.close)
                client = EvidenceClient(ServiceEndpoint(endpoint, EVIDENCE_METHODS))
                client.pipeline_reranker = ranker
                result = client.search_pipeline(authority.actor, authority.scope, 'Mira Vale birth date', {},
                    {'channels': ['bm25'], 'rerank': True, 'parent': 'none', 'title_navigation': False},
                    assessment_question='When was the director of Unseen Film born?')
                self.assertEqual(len(result['hits']), expected_count)
                self.assertFalse(result['coverage']['requirements'][0]['supported'])
                self.assertEqual(result['coverage']['protocol'], protocol)
                if expected_count:
                    self.assertEqual(result['hits'][0]['text'], 'Mira Vale was born in 1972.')

    def test_reference_assessment_rejects_unknown_empty_and_regenerated_quotes(self):
        from kb_platform.reranking import validate_coverage, RerankValidationError
        from kb_platform.retrieval_assessment import unpack_assessment

        def requirement(evidence):
            return [{'need': 'explicit relation', 'supported': True, 'evidence': evidence}]

        self.assertTrue(validate_coverage(requirement([{'ref': 'R001'}]), {'R001': 'Original'},
                                         protocol='coverage-references/2'))
        for evidence, sources in [([{'ref': 'R999'}], {'R001': 'Original'}),
                ([{'ref': 'R001'}], {'R001': ''}),
                ([{'ref': 'R001', 'quote': 'Original'}], {'R001': 'Original'})]:
            with self.subTest(evidence=evidence, sources=sources):
                with self.assertRaises(RerankValidationError):
                    validate_coverage(requirement(evidence), sources, protocol='coverage-references/2')
        candidates = [{'citation_id': 'allowed', 'text': 'Original'}]
        value = {'ranking': [{'citation_id': 'allowed', 'score': 1.0}],
                 'coverage_protocol': 'coverage-references/2',
                 'coverage': requirement([{'citation_id': 'allowed'}])}
        self.assertEqual(unpack_assessment(value, candidates)[1], value['coverage'])
        value['coverage'][0]['evidence'][0]['citation_id'] = 'other-scope'
        with self.assertRaises(Exception) as caught:
            unpack_assessment(value, candidates)
        self.assertEqual(caught.exception.code, 'INVALID_RERANK_OUTPUT')

    def test_assessment_crosses_json_and_does_not_admit_high_ranked_namesake(self):
        authority = Authority([('Director.md', 'Alex Vale is a Canadian film director.'),
                               ('Drummer.md', 'Alex Vale is an American drummer.')])

        class Model:
            provider_id = 'magpie_luna'
            manifest = {'model': 'synthetic-offline'}

            def complete_json(self, system, payload, **kwargs):
                documents = json.loads(payload)['documents']
                source = next(row for row in documents if 'Canadian' in row['text'])
                return {'scores': [0.2 if 'Canadian' in row['text'] else 0.99 for row in documents],
                    'requirements': [{'need': 'film director nationality', 'supported': True,
                        'evidence': [{'ref': source['ref']}]},
                        {'need': 'other director nationality', 'supported': False, 'evidence': []}]}

        ranker = LunaCandidateReranker(Model())
        gateway = Gateway(authority, pipeline_reranker=ranker)
        endpoint = EvidenceEndpoint(gateway)
        self.addCleanup(endpoint.close)
        client = EvidenceClient(ServiceEndpoint(endpoint, EVIDENCE_METHODS))
        client.pipeline_reranker = ranker
        result = client.search_pipeline(authority.actor, authority.scope, 'Alex Vale director', {},
            {'channels': ['bm25'], 'rerank': True, 'top_k': 5, 'parent': 'none', 'title_navigation': True})
        self.assertEqual([item['text'] for item in result['hits']], ['Alex Vale is a Canadian film director.'])
        self.assertEqual([r['supported'] for r in result['coverage']['requirements']], [True, False])
        self.assertEqual(result['coverage']['status'], 'observed')
        self.assertTrue(result['coverage']['unadmitted_citation_ids'])

        from dataclasses import fields
        from kb_platform.agent_builtin_tools import BuiltinTools

        saved = []
        question = 'What nationality is the film director Alex Vale?'
        tool = BuiltinTools(**{field.name: None for field in fields(BuiltinTools)})
        tool.actor, tool.scope = authority.actor, authority.scope
        tool.state = {'source_policy': {}, 'evidence': {}}
        tool.config, tool.trace, tool.seen_turn_evidence = {}, {}, set()
        tool.turn_gateway = client
        tool.retrieval_config = {'rerank': True, 'parent': 'none', 'title_navigation': True}
        tool.max_active_evidence = 5
        tool.assessment_question = question
        tool.invoke_tool = lambda action, args, call, **kwargs: call()
        tool.save_evidence = saved.extend
        tool.search('search', {'query': 'Alex Vale director'}, 'lexical')
        self.assertEqual([item['text'] for item in saved], ['Alex Vale is a Canadian film director.'])
        self.assertEqual(tool.trace['retrieval_stages'][0]['config']['channels'], ['bm25'])
        prepared = ContextManager(ContextPolicy(input_budget=24000)).prepare(
            'answer', '', {}, [], saved, question)
        self.assertNotIn('American drummer', json.dumps(prepared.messages))
        self.assertIn('Canadian film director', json.dumps(prepared.messages))

    def test_invalid_or_foreign_coverage_cannot_become_evidence(self):
        candidates = [{'citation_id': 'a', 'text': 'Verified original.'}]
        valid = {'ranking': [{'citation_id': 'a', 'score': 0.5}], 'coverage': [
            {'need': 'fact', 'supported': True, 'evidence': [{'citation_id': 'a', 'quote': 'Verified original.'}]}]}
        for change in ({'citation_id': 'foreign'}, {'quote': 'Invented.'}, {'quote': ''}):
            invalid = deepcopy(valid)
            invalid['coverage'][0]['evidence'][0].update(change)
            with self.assertRaises(Exception) as caught:
                unpack_assessment(invalid, candidates)
            self.assertEqual(caught.exception.code, 'INVALID_RERANK_OUTPUT')
        ranking, coverage = unpack_assessment(valid['ranking'], candidates)
        self.assertEqual(ranking, valid['ranking'])
        self.assertIsNone(coverage)

    def test_workflow_keeps_batch_coverage_for_planning_not_answer_or_verification(self):
        case = self.setup_service([('decision', fixtures.decision('search_pipeline')),
            ('retrieval', fixtures.decision('answer')), ('answer', fixtures.cited_answer),
            ('verifier', fixtures.verdict())])
        case.gateway.pipeline_reranker = Reranker()
        case.service.retrieval_config = {'channels': ['bm25'], 'rerank': True, 'parent': 'none'}
        original_rank = case.gateway.pipeline_reranker.rank

        def assess(query, candidates):
            return {'ranking': original_rank(query, candidates), 'coverage': [
                {'need': 'original fact', 'supported': True, 'evidence': [
                    {'citation_id': row['citation_id'], 'quote': row['text']} for row in candidates]},
                {'need': 'unavailable fact', 'supported': False, 'evidence': []}]}

        case.gateway.pipeline_reranker.assess = assess
        result = self.turn(case)
        self.assertEqual(result['status'], 'answered', result)
        planning_call = next(row for row in case.model.calls if row['role'] == 'retrieval')
        projection = json.loads(planning_call['messages'][-1]['content'])
        coverage = projection['state']['tool_results'][0]['result']['coverage']
        self.assertEqual(coverage['requirements'][-1]['supported'], False)
        self.assertEqual(coverage['requirements'][0]['need'], 'original fact')
        self.assertNotIn('quote', json.dumps(coverage))
        for role in ('answer', 'verifier'):
            call = next(row for row in case.model.calls if row['role'] == role)
            projection = json.loads(call['messages'][-1]['content'])
            self.assertNotIn('coverage', projection['state']['tool_results'][0]['result'])
            self.assertTrue(projection['evidence'])
        self.assertIn('coverage', result['trace']['tool_calls'][0]['result'])

    def test_role_projection_preserves_factual_tools_and_does_not_mutate_receipts(self):
        from kb_platform.agent_tool_view import project_tool_results

        results = [
            {'name': 'search_pipeline', 'status': 'ok', 'result': {
                'coverage': {'requirements': [{'need': 'date', 'supported': False}]},
                'evidence_ids': ['new-date'], 'long': 'x' * 4000}},
            {'name': 'calculator', 'status': 'ok', 'result': {'value': 42}},
            {'name': 'read_history', 'status': 'ok', 'result': {
                'events': [{'seq': 1, 'text': 'My budget is 42.'}]}},
            {'name': 'business_metric', 'status': 'ok', 'result': {'coverage': 0.8}},
        ]
        original = deepcopy(results)
        for size in (2000, 20000):
            for role in ('answer', 'verifier'):
                view = project_tool_results(results, budget_bytes=size, role=role)
                self.assertNotIn('coverage', view[0]['result'])
                self.assertEqual(view[0]['result']['evidence_ids'], ['new-date'])
                self.assertEqual(view[1:], results[1:])
            view = project_tool_results(results, budget_bytes=size, role='retrieval')
            self.assertEqual(view[0]['result']['coverage'], results[0]['result']['coverage'])
        self.assertEqual(results, original)

    def test_new_search_results_do_not_jump_ahead_of_retained_evidence(self):
        old = {'citation_id': 'old', 'source_id': 's0', 'source_version_id': 'v0', 'text': 'prior fact'}
        new = {'citation_id': 'new', 'source_id': 's1', 'source_version_id': 'v1', 'text': 'new candidate'}
        turn = SimpleNamespace(service=SimpleNamespace(store=SimpleNamespace(artifact=Mock(return_value={}))),
            actor=None, session_id='session', token='token', event=Mock(), max_active_evidence=3,
            turn_lineage={}, state={'evidence': {'old': old}, 'evidence_order': ['old'],
                'evidence_offsets': {}, 'evidence_catalog': {}, 'checkpoints': {},
                'source_policy': {'version_policy': 'latest'}})
        KnowledgeTurn.save_evidence(turn, [new])
        self.assertEqual(list(turn.state['evidence']), ['old', 'new'])
        self.assertEqual(turn.state['evidence']['old'], old)
        KnowledgeTurn.save_evidence(turn, [new], prioritize=True)
        self.assertEqual(list(turn.state['evidence']), ['new', 'old'])


if __name__ == '__main__':
    unittest.main()
