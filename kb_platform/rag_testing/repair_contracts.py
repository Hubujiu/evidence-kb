"""Offline safety checks for the experimental source-title packet component."""
import unittest

from kb_platform.title_packets import title_packet


def doc(identity, title, text):
    return dict(source_id=identity, title=title, text=text)


class PacketContracts(unittest.TestCase):
    def test_two_hop_navigation_keeps_verbatim_sources(self):
        sources = [doc('film', 'Example Film', 'Example Film was directed by Jane Smith.'),
                   doc('person', 'Jane Smith', 'Jane Smith was born in 1953.'),
                   doc('noise', 'Jane Smithson', 'Unrelated.')]
        packet, edges = title_packet('Who directed Example Film?', sources)
        self.assertEqual(packet, sources[:2])
        self.assertTrue(all(not edge['relation_verified'] for edge in edges))

    def test_ambiguous_titles_not_resolved(self):
        sources = [doc('a', 'Jane Smith', 'First'), doc('b', 'Jane Smith', 'Second')]
        self.assertEqual(title_packet('Jane Smith', sources)[0], [])

    def test_disambiguation_parenthetical_not_discarded(self):
        sources = [doc('a', 'Tony Williams (drummer)', 'An American drummer.')]
        self.assertEqual(title_packet('Tony Williams', sources)[0], [])

    def test_boundary_not_substring(self):
        self.assertEqual(title_packet('Jane Smithson', [doc('a', 'Jane Smith', 'Text')])[0], [])

    def test_bounded_cycle(self):
        sources = [doc('a', 'First Name', 'Second Name'), doc('b', 'Second Name', 'First Name')]
        self.assertEqual(len(title_packet('First Name', sources, limit=1)[0]), 1)
        self.assertEqual(len(title_packet('First Name', sources)[0]), 2)

    def test_unknown_source_not_invented(self):
        sources = [doc('a', 'First Name', 'Missing Person')]
        self.assertEqual(title_packet('First Name', sources)[0], sources)

    def test_invalid_limits(self):
        for kwargs in ({'limit': True}, {'hops': -1}, {'limit': 21}):
            with self.assertRaises(ValueError):
                title_packet('question', [], **kwargs)

    def test_punctuation_collision_requires_literal_match(self):
        docs = [doc('old', 'Johnny-on-the-Spot', 'Old movie'), doc('new', 'Johnny on the Spot', 'New movie')]
        self.assertEqual([d['source_id'] for d in title_packet('Johnny-On-The-Spot', docs)[0]], ['old'])

    def test_initials_navigation_is_only_a_hint(self):
        docs = [doc('a', 'Donald Winnicott', '# Donald Winnicott\nDonald Woods Winnicott (1896) was English.')]
        packet, edges = title_packet('D. W. Winnicott', docs)
        self.assertEqual(packet, docs)
        self.assertEqual(edges[0]['hint_kind'], 'initials_search_hint')
        self.assertFalse(edges[0]['relation_verified'])

    def test_ambiguous_initials_not_resolved(self):
        docs = [doc('a', 'Donald Winnicott', '# title\nDonald Woods Winnicott (1896)'),
                doc('b', 'David Winnicott', '# title\nDavid Walter Winnicott (1900)')]
        self.assertEqual(title_packet('D. W. Winnicott', docs)[0], [])


class EvidenceObjectiveContracts(unittest.TestCase):
    def test_old_plan_hash_is_stable(self):
        import json
        from pathlib import Path
        from .contracts import Plan, digest
        raw = json.loads(Path('config/rag-testing/plan-v1.json').read_text('utf-8'))
        raw.setdefault('require_answer_behavior', False)  # Already an old Plan default.
        self.assertEqual(digest(raw), digest(Plan.model_validate(raw)))

    def test_evidence_objective_is_hash_bound(self):
        from .contracts import Plan, digest
        a = Plan(suite_hash='test')
        b = Plan(suite_hash='test', objective='evidence')
        self.assertNotEqual(digest(a), digest(b))

    def test_no_answer_behavior_does_not_hide_evidence_failure(self):
        import json
        from pathlib import Path
        from .contracts import Suite, Plan, Trace, digest
        from .scoring import evaluate
        suite = Suite.model_validate_json(Path('config/rag-testing/suite-v1.json').read_text('utf-8'))
        raw = json.loads(Path('config/rag-testing/oracle-traces-v1.json').read_text('utf-8'))
        traces = [Trace.model_validate(t) for t in raw]
        broken = [t.model_copy(update={'behavior': None, 'final': (), 'final_context': '',
                   'request_messages': ({'role': 'user', 'content': ''},)}) for t in traces]
        result = evaluate(suite, broken, Plan(suite_hash=digest(suite), objective='evidence'))
        self.assertGreater(result['counts']['FAIL'], 0)


class PackingContracts(unittest.TestCase):
    def test_oversized_unmatched_table_is_not_truncated(self):
        from kb_platform.agent_context import ContextManager, ContextPolicy
        item = dict(citation_id='c', source_id='s', text='| row | value |\n' * 1000, kind='Table')
        result = ContextManager(ContextPolicy(input_budget=4000, evidence_strategy='query_blocks')).prepare(
            'answer', 'system', {}, [], [item], 'zzzzz')
        self.assertFalse(result.projection['evidence'][0].get('text'))


class ProductNavigationContracts(unittest.TestCase):
    def execute_fixture(self, *, maximum=50, excluded=(), enabled=True):
        from types import SimpleNamespace
        from unittest.mock import patch
        from kb_platform.retrieval_pipeline import execute
        documents = [doc('a', 'Harbor Guide', 'Harbor Guide requires Bridge Guide.'),
                     doc('b', 'Bridge Guide', 'Valid only until 2030.')]
        units = {d['source_id']: dict(citation_id=d['source_id'], source=d, start=0,
            end=len(d['text']), element=dict(kind='Paragraph', raw_text=d['text'])) for d in documents}
        gateway = SimpleNamespace(embedding_provider=None, pipeline_reranker=None, research=None,
            platform=SimpleNamespace(_build_config=lambda _: {'channels': ['bm25']}),
            _ids=lambda _: None, _allowed=lambda *args: [],
            _state=lambda *args, **kwargs: ({'manifest_id': 'build'}, [], {}),
            _unchanged=lambda *args: None,
            read=lambda actor, scope, ids, policy: [dict(citation_id=k, source_id=k, text=units[k]['element']['raw_text']) for k in ids])
        with patch('kb_platform.retrieval_pipeline._catalog', return_value=(documents, units)):
            return execute(gateway, SimpleNamespace(tenant_id='t'), 'kb', 'Harbor Guide', {},
                dict(title_navigation=enabled, top_k=8, max_expanded_units=maximum), exclude_citation_ids=excluded)

    def test_product_path_uses_canonical_read(self):
        result = self.execute_fixture()
        self.assertEqual([h['citation_id'] for h in result['hits']], ['a', 'b'])
        self.assertFalse(result['trace']['title_navigation']['relations_verified'])

    def test_unit_budget_omission_visible(self):
        result = self.execute_fixture(maximum=1)
        self.assertEqual(len(result['hits']), 1)
        self.assertEqual(result['trace']['expansion'][1]['status'], 'OMITTED_UNIT_BUDGET')

    def test_excluded_unit_not_resurrected(self):
        result = self.execute_fixture(excluded=('b',))
        self.assertEqual([h['citation_id'] for h in result['hits']], ['a'])
        self.assertEqual(result['trace']['expansion'][1]['status'], 'EXCLUDED_EXISTING_UNITS')

    def test_default_path_does_not_change_selection(self):
        result = self.execute_fixture(enabled=False)
        self.assertEqual(result['hits'], [])
        self.assertIsNone(result['trace']['title_navigation'])


class SectionNavigationContracts(unittest.TestCase):
    def test_section_reference_followed_by_prose(self):
        from kb_platform.cross_document import navigate_source_references
        docs = [dict(id='a', title='入口', text='§入口：请查《目标》§作业执行，不使用附录。'),
                dict(id='b', title='目标', text='§作业：先核对，再批准。\n\n§作业附录：无关。')]
        result = navigate_source_references(docs, ['a'])
        targets = [f['text'] for f in result['fragments'] if f['doc_id'] == 'b']
        self.assertEqual(targets, ['§作业：先核对，再批准。'])

    def test_longest_existing_heading_and_table_kept(self):
        from kb_platform.cross_document import navigate_source_references
        body = '§报价：旧版。\n\n§报价修订：单位CNY/件。\n| 编码 | 价格 |\n| A | 13 |'
        docs = [dict(id='a', title='入口', text='§入口：查《目标》§报价修订内的编码。'),
                dict(id='b', title='目标', text=body)]
        result = navigate_source_references(docs, ['a'])
        self.assertEqual([f['text'] for f in result['fragments'] if f['doc_id'] == 'b'], [body.split('\n\n')[1]])


if __name__ == '__main__':
    unittest.main()
