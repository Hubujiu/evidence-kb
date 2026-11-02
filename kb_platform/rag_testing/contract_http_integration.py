import json
import os
import unittest
from unittest.mock import patch

from kb_platform.settings import Settings

from .contract_application_contracts import ContractApplicationContracts


class ContractHTTPIntegration(unittest.TestCase):
    setUp = ContractApplicationContracts.setUp

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
