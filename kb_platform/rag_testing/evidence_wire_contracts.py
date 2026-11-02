import unittest

from kb_platform.knowledge_contracts import EVIDENCE_METHODS, EvidenceClient, EvidenceEndpoint
from kb_platform.plugin_contracts import ServiceEndpoint

from .product_fixture import Authority, Gateway


class EvidenceWireContracts(unittest.TestCase):
    def setUp(self):
        self.authority = Authority([
            ('Orders.csv', 'item,quantity\nA,2\nB,3\nA,5\n'),
            ('Prices.csv', 'item,price\nA,10\nB,20\n'),
            ('Workflow.csv', 'from_step,to_step,condition_key,condition_value\n'
                             'intake,review,licensed,yes\nreview,__END__,,\n'),
        ])
        self.endpoint = EvidenceEndpoint(Gateway(self.authority))
        self.addCleanup(self.endpoint.close)
        self.client = EvidenceClient(ServiceEndpoint(self.endpoint, EVIDENCE_METHODS))
        self.actor, self.scope = self.authority.actor, self.authority.scope
        self.tables = {record['source_id']: citation for citation, record in self.authority.records.items()
                       if record.get('table_data')}

    def test_table_rows_count_filters_and_sum_cross_json_with_nullable_column(self):
        citation = self.tables['s0']
        rows = self.client.table(self.actor, self.scope, citation, {}, 'rows', None, None)
        self.assertEqual(rows['result'], [['A', '2'], ['B', '3'], ['A', '5']])
        self.assertEqual(rows['evidence']['citation_id'], citation)
        self.assertEqual(rows['source_row_numbers'], [2, 3, 4])
        filtered = self.client.table(self.actor, self.scope, citation, {}, 'rows', None,
                                     [{'column': 'item', 'op': 'eq', 'value': 'A'}])
        self.assertEqual(filtered['result'], [['A', '2'], ['A', '5']])
        self.assertEqual(filtered['source_row_numbers'], [2, 4])
        counted = self.client.table(self.actor, self.scope, citation, {}, operation='count',
                                    column=None, filters=[])
        self.assertEqual(counted['result'], 3)
        total = self.client.table(self.actor, self.scope, citation, {}, operation='sum', column='quantity',
                                 filters=[{'column': 'quantity', 'op': 'gte', 'value': 3}])
        self.assertEqual(total['result'], '8')
        self.assertEqual(total['source_row_numbers'], [3, 4])

    def test_workflow_null_or_empty_step_clarifies_and_explicit_facts_select_original_edge(self):
        citation = self.tables['s2']
        for current_step in (None, ''):
            with self.subTest(current_step=current_step):
                result = self.client.workflow_next(self.actor, self.scope, citation, {},
                                                   current_step=current_step, facts=None)
                self.assertEqual(result['status'], 'clarify')
                self.assertEqual(result['missing_info'], ['current_step'])
                self.assertEqual(result['evidence']['citation_id'], citation)
        ready = self.client.workflow_next(self.actor, self.scope, citation, {},
                                          current_step='intake', facts={'licensed': 'yes'})
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['next_steps'], ['review'])
        self.assertEqual(ready['edges'][0]['source_row_number'], 2)
        complete = self.client.workflow_next(self.actor, self.scope, citation, {},
                                             current_step='review', facts={})
        self.assertEqual(complete['status'], 'complete')
        self.assertEqual(complete['next_steps'], [])

    def test_table_search_read_revalidation_and_join_keep_original_lineage(self):
        hits = self.client.search_tables(self.actor, self.scope, 'item', {}, limit=5,
                                         exclude_citation_ids=())
        self.assertEqual({hit['citation_id'] for hit in hits}, set(self.tables.values()))
        citations = [self.tables['s0'], self.tables['s1']]
        originals = self.client.read(self.actor, self.scope, citations, {})
        self.assertEqual(self.client.revalidate(self.actor, self.scope, originals, {}), originals)
        result = self.client.table_join(self.actor, self.scope, citations, {},
            left_key='item', right_key='item', how='inner', max_rows=10)
        self.assertEqual(result['returned_rows'], 3)
        self.assertEqual(result['left_source_row_numbers'], [2, 3, 4])
        self.assertEqual(result['right_source_row_numbers'], [2, 3, 2])
        self.assertEqual([row['citation_id'] for row in result['evidence']], citations)
        self.assertEqual(result['rows'][0], {'left.item': 'A', 'left.quantity': '2',
                                           'right.item': 'A', 'right.price': '10'})
