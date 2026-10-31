import unittest

from kb_platform.planning_references import (bind_planning_schema, planning_bindings,
                                             project_references, resolve_planning_decision)
from kb_platform.agent_schema import compile_schema, validate_output
from kb_platform.agent_tool_registry import bind_decision_evidence, decision_schema


class PlanningReferenceContracts(unittest.TestCase):
    def test_handles_bind_exactly_without_rewriting_source_text_or_identity(self):
        identities = ['544be6f7-e8b5-5b81-aa84-df977e306076', '272cbb37-ecef-5ae6-8915-37515cc914ab']
        mapping = planning_bindings(identities)
        evidence = {'citation_id': identities[0], 'text': identities[0], 'source_id': identities[0],
                    'nested': {'evidence_ids': identities}}
        projected = project_references(evidence, mapping)
        self.assertEqual(projected['citation_id'], 'E2')
        self.assertEqual(projected['text'], evidence['text'])
        self.assertEqual(projected['source_id'], evidence['source_id'])
        decision = resolve_planning_decision({'action': 'table_join', 'citation_ids': ['E2', 'E1']}, mapping)
        self.assertEqual(decision['citation_ids'], identities)
        self.assertEqual(evidence['citation_id'], identities[0])

    def test_mixed_or_unknown_handles_are_rejected_without_fuzzy_repair(self):
        mapping = planning_bindings(['first-uuid', 'second-uuid'])
        for reference in ['E3', 'first-uuid', 'first-second', 'e1']:
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                resolve_planning_decision({'citation_ids': [reference]}, mapping)

    def test_schema_admits_only_current_handles_and_preserves_required_evidence(self):
        canonical = bind_decision_evidence(decision_schema({}, set(), False), ['raw-one', 'raw-two'])
        schema = bind_planning_schema(canonical, planning_bindings(['raw-one', 'raw-two']))
        validate_output({'intent': 'knowledge', 'action': 'read', 'citation_ids': ['E1']}, compile_schema(schema))
        for value in [{'intent': 'knowledge', 'action': 'read'},
                      {'intent': 'knowledge', 'action': 'read', 'citation_ids': ['raw-one']}]:
            with self.assertRaises(ValueError):
                validate_output(value, compile_schema(schema))
        self.assertEqual(canonical['properties']['citation_ids']['items']['enum'], ['raw-one', 'raw-two'])
