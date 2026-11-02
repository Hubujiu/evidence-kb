import json
import unittest

from scripts.generate_application_contracts import DEFAULT_OUTPUT, contract_document


class PublicContractDocumentContracts(unittest.TestCase):
    def test_published_machine_readable_contracts_match_runtime_declarations(self):
        expected = json.dumps(contract_document(), ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        self.assertEqual(DEFAULT_OUTPUT.read_text('utf-8'), expected,
                         'Regenerate with python scripts/generate_application_contracts.py')

    def test_each_builtin_export_and_dependency_has_a_published_versioned_contract(self):
        document = contract_document()
        contracts = {(row['id'], row['major']) for row in document['contracts']}
        schemas = {(row['implementation'], row['version']) for row in document['schemas']}
        for definition in document['catalog']:
            with self.subTest(implementation=definition['implementation']):
                self.assertIn((definition['implementation'], definition['version']), schemas)
                for contract in [*definition['provides'].values(), *(
                        item['contract'] for item in definition['requires'].values())]:
                    self.assertIn((contract['id'], contract['major']), contracts)

    def test_authority_contract_publishes_portable_dataset_structure(self):
        document = contract_document()
        authority = next(row for row in document['contracts'] if row['id'] == 'knowledge.authority.storage')
        schema = authority['dataset_schema']
        self.assertTrue(schema['tables'])
        self.assertNotIn('owner', schema)
        self.assertNotIn('digest', schema)
        self.assertEqual(set(authority['methods']), {'describe', 'relational'})
