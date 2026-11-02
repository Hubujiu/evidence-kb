import json
import os
import unittest
from unittest.mock import patch

from kb_platform.settings import Settings

from .contract_application_contracts import ContractApplicationContracts


class ContractHTTPIntegration(unittest.TestCase):
    setUp = ContractApplicationContracts.setUp

    def test_large_parsed_document_upload_publish_search_and_original_download(self):
        from fastapi.testclient import TestClient
        from kb_platform.documents import max_upload_bytes
        from kb_platform.storage import OriginalStore

        with patch.dict(os.environ, {'KB_DEFER_DEFAULT_APP': '1'}):
            from kb_platform.api import create_app

            app = create_app(self.settings, profile=self.profile, models=self.models,
                             storage_configuration=self.storage)
        headers = {'Authorization': 'Bearer ' + self.token}
        content = (b'Evidence line with number 42.\n' * 4000)[:100000]
        self.assertLess(len(content), max_upload_bytes())
        with TestClient(app) as client:
            created = client.post('/scopes', headers=headers, json={'name': 'Large parse JSON boundary'})
            self.assertEqual(created.status_code, 201, created.text)
            scope = created.json()['scope_id']
            uploaded = client.post(f'/knowledge-bases/{scope}/sources', headers=headers,
                files={'file': ('100KB.txt', content, 'text/plain')},
                data={'policy': json.dumps({'llm_context': True, 'allowed_providers': ['synthetic']})})
            self.assertEqual(uploaded.status_code, 201, uploaded.text[:1000])
            document = uploaded.json()['document']
            self.assertEqual(document['status'], 'READY')
            self.assertGreater(len(json.dumps(document).encode()), 4 * 1024 * 1024)
            self.assertGreater(len(document['elements']), 1)
            staged = client.post(f'/knowledge-bases/{scope}/builds', headers=headers, json={'channels': ['bm25']})
            self.assertEqual(staged.status_code, 201, staged.text)
            published = client.post(f'/knowledge-bases/{scope}/builds/{staged.json()["build_id"]}/publish',
                                    headers=headers)
            self.assertEqual(published.status_code, 200, published.text)
            found = client.post(f'/knowledge-bases/{scope}/search', headers=headers, json={'query': 'number 42'})
            self.assertEqual(found.status_code, 200, found.text)
            self.assertTrue(found.json()['hits'])
            citation = found.json()['hits'][0]['citation_id']
            reads, read = [], OriginalStore.read

            def counted(store, *args, **kwargs):
                reads.append(args)
                return read(store, *args, **kwargs)

            with patch.object(OriginalStore, 'read', counted):
                original = client.get(f'/knowledge-bases/{scope}/citations/{citation}/original', headers=headers)
            self.assertEqual(original.status_code, 200, original.text[:1000])
            self.assertEqual(original.content, content)
            self.assertEqual(len(reads), 1)
            self.assertEqual(self.physical, [])
        self.assertEqual(app.state.root_plugins.inspect()['state'], 'disposed')

    def test_http_uses_access_plugin_and_delivers_full_turn_without_private_identity_loader(self):
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {'KB_DEFER_DEFAULT_APP': '1'}):
            from kb_platform.api import create_app

            with patch.object(Settings, 'identities', side_effect=AssertionError('PRIVATE_IDENTITY_LOADER_USED')):
                app = create_app(self.settings, profile=self.profile, models=self.models,
                                 storage_configuration=self.storage)
        headers = {'Authorization': 'Bearer ' + self.token}
        with TestClient(app) as client:
            missing = client.get('/identity')
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.headers['www-authenticate'], 'Bearer')
            rejected = client.get('/identity', headers={'Authorization': 'Bearer invalid'})
            self.assertEqual(rejected.status_code, 401, rejected.text)
            self.assertEqual(rejected.json()['error'], 'AUTHENTICATION_FAILED')
            identity = client.get('/identity', headers=headers)
            self.assertEqual(identity.status_code, 200, identity.text)
            self.assertEqual(identity.json(), {'tenant_id': self.actor.tenant_id,
                                               'principal_id': self.actor.principal_id})
            response = client.post('/scopes', json={'name': 'Synthetic HTTP acceptance'}, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            scope = response.json()['scope_id']
            upload = client.post(f'/knowledge-bases/{scope}/sources', headers=headers,
                files={'file': ('Original.md', b'Original rule value 42.', 'text/markdown')},
                data={'policy': json.dumps({'llm_context': True, 'allowed_providers': ['synthetic']})})
            self.assertEqual(upload.status_code, 201, upload.text)
            staged = client.post(f'/knowledge-bases/{scope}/builds', headers=headers, json={'channels': ['bm25']})
            self.assertEqual(staged.status_code, 201, staged.text)
            published = client.post(f'/knowledge-bases/{scope}/builds/{staged.json()["build_id"]}/publish',
                                    headers=headers)
            self.assertEqual(published.status_code, 200, published.text)
            session = client.post(f'/scopes/{scope}/sessions', headers=headers, json={})
            self.assertEqual(session.status_code, 201, session.text)
            endpoint = f'/agent/sessions/{session.json()["session_id"]}/turns'
            request = {'message': 'What is the original rule?', 'request_id': 'http-durable-request',
                       'expected_revision': 0, 'constraints': ['Use original sources']}
            result = client.post(endpoint, headers=headers, json=request)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'answered', result.text)
            self.assertEqual(result.json()['answer'], 'Original rule value 42.')
            repeated = client.post(endpoint, headers=headers, json=request)
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(repeated.json(), result.json())
            self.assertEqual(len(self.physical), 4)
            self.assertEqual(result.headers['cache-control'], 'no-store')
        self.assertEqual(app.state.root_plugins.inspect()['state'], 'disposed')
