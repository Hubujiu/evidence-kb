from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.application_knowledge import DEFAULT_APPLICATION, create_contract_knowledge
from kb_platform.application_storage import ApplicationStorage
from kb_platform.core import Platform
from kb_platform.database_contracts import DatabaseError
from kb_platform.knowledge_access import KnowledgeAccess
from kb_platform.knowledge_authority_contract import (
    METHODS, AuthorityStorageEndpoint, authority_database, authority_schema_descriptor,
)
from kb_platform.knowledge_storage import KnowledgeRepository
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_native import PluginError
from kb_platform.session_identity import Actor
from kb_platform.settings import Settings
from kb_platform.storage_catalog import initialize_configuration


class KnowledgeAuthorityContracts(unittest.TestCase):
    def test_foreign_authority_revision_is_negotiated_from_neutral_schema(self):
        with TemporaryDirectory() as directory:
            configuration = initialize_configuration({'version': 2, 'application_id': 'foreign-authority-contract',
                'instances': {'records': {'provider': 'sqlite', 'config': {'root': directory}}},
                'services': {'records': {'instance': 'records', 'export': 'structured'}},
                'bindings': {'repository': {'primary': {
                    'service': 'records', 'owner': 'authority-owner', 'space': 'authority'}}}})
            storage = ApplicationStorage(configuration)
            try:
                endpoint = ServiceEndpoint(AuthorityStorageEndpoint(storage), METHODS)
                original_digest = endpoint.describe({})['schema']['digest']
                prepares = []

                def foreign_schema(schema):
                    schema['digest'] = 'foreign-authority-revision:9'
                    schema['tables'].reverse()
                    for table in schema['tables']:
                        table['constraints'].reverse()
                    return schema

                def describe(request):
                    response = endpoint.describe(request)
                    response['schema'] = foreign_schema(response['schema'])
                    return response

                def relational(request):
                    if request['op'] == 'prepare':
                        prepares.append(request['digest'])
                        self.assertEqual(request['digest'], 'foreign-authority-revision:9')
                        return endpoint.relational({**request, 'digest': original_digest})
                    response = endpoint.relational(request)
                    if request['op'] == 'schemas':
                        response['schemas'] = [foreign_schema(schema) for schema in response['schemas']]
                    return response

                foreign = ServiceEndpoint(SimpleNamespace(describe=describe, relational=relational), METHODS)
                repository = KnowledgeRepository(database=authority_database(foreign))
                platform = Platform(repository=repository, search=None, store=None, parser=None, embedding_jobs=None)
                actor = Actor('tenant', 'owner')
                scope = platform.create_kb(actor, 'Foreign protocol authority')['kb_id']
                another = KnowledgeRepository(database=authority_database(foreign))
                self.assertEqual(KnowledgeAccess(another).authorize({'actor': asdict(actor), 'scope_id': scope}),
                                 {'scope_id': scope, 'manifest_id': None, 'authority_revision': 1})
                self.assertEqual(prepares, ['foreign-authority-revision:9', 'foreign-authority-revision:9'])
            finally:
                storage.close()

    def test_public_dataset_descriptor_and_json_operations_share_current_authority(self):
        with TemporaryDirectory() as directory:
            configuration = initialize_configuration({'version': 2, 'application_id': 'authority-contract',
                'instances': {'records': {'provider': 'sqlite', 'config': {'root': directory}}},
                'services': {'records': {'instance': 'records', 'export': 'structured'}},
                'bindings': {'repository': {'primary': {
                    'service': 'records', 'owner': 'authority-owner', 'space': 'authority'}},
                    'store': {'primary': {'service': 'records', 'owner': 'sessions', 'space': 'history'}}}})
            storage = ApplicationStorage(configuration)
            try:
                endpoint = ServiceEndpoint(AuthorityStorageEndpoint(storage), METHODS)
                described = endpoint.describe({})
                self.assertEqual(described['identity']['space']['owner_id'], 'authority-owner')
                self.assertEqual(described['schema'], authority_schema_descriptor('authority-owner'))
                neutral = authority_schema_descriptor()
                self.assertNotIn('owner', neutral)
                self.assertNotIn('digest', neutral)
                self.assertEqual(set(neutral['appendonly']), {'source_versions', 'parse_snapshots', 'citations'})
                tables = {table['name'] for table in neutral['tables']}
                self.assertTrue({'tenant_state', 'knowledge_bases', 'members', 'sources'} <= tables)
                repository = KnowledgeRepository(database=authority_database(endpoint))
                platform = Platform(repository=repository, search=None, store=None, parser=None,
                                    embedding_jobs=None)
                actor = Actor('tenant', 'owner')
                scope = platform.create_kb(actor, 'Published authority contract')['kb_id']
                another = KnowledgeRepository(database=authority_database(endpoint))
                self.assertEqual(KnowledgeAccess(another).authorize({'actor': asdict(actor), 'scope_id': scope}),
                    {'scope_id': scope, 'manifest_id': None, 'authority_revision': 1})
                with self.assertRaises(ContractError):
                    endpoint.relational({'version': 1, 'op': 'schemas', 'binding': 'store.primary'})
                self.assertNotIn('store.primary', storage._structured)
            finally:
                storage.close()

    def test_foreign_authority_schema_must_match_public_version_before_queries(self):
        descriptor = authority_schema_descriptor('owner')
        descriptor['tables'] = []
        dependency = SimpleNamespace(describe=lambda request: {
            'identity': {'space': {'application_id': 'app', 'owner_id': 'owner', 'space_id': 'authority'},
                         'dataset_id': 'dataset'}, 'methods': ['relational'], 'schema': descriptor},
            relational=Mock())
        with self.assertRaisesRegex(DatabaseError, 'KNOWLEDGE_AUTHORITY_SCHEMA_MISMATCH'):
            authority_database(dependency)
        dependency.relational.assert_not_called()

    def test_builtin_data_and_access_mismatched_authority_exports_fail_before_factories(self):
        profile = json.loads(DEFAULT_APPLICATION.read_text('utf-8'))
        profile['plugins']['other-storage'] = deepcopy(profile['plugins']['storage'])
        profile['bindings']['access']['authority']['plugin'] = 'other-storage'
        with patch('kb_platform.application_knowledge.ApplicationStorage') as factory:
            with self.assertRaises(PluginError) as caught:
                create_contract_knowledge(Settings(Path('unused'), Path('unused')), profile=profile)
        self.assertEqual(caught.exception.code, 'KNOWLEDGE_AUTHORITY_BINDING_MISMATCH')
        factory.assert_not_called()
