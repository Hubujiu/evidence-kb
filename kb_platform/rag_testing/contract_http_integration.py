import json
import os
import unittest
from unittest.mock import patch

from kb_platform.settings import Settings

from .contract_application_contracts import ContractApplicationContracts


class ContractHTTPIntegration(unittest.TestCase):
    setUp = ContractApplicationContracts.setUp

    def test_response_loss_after_unconfirmed_settlement_recovers_durable_observations(self):
        from fastapi.testclient import TestClient
        from kb_platform.database_contracts import CommitUnknown

        original_post = self.client._post

        def unconfirmed(*args, **kwargs):
            body = original_post(*args, **kwargs)
            error = CommitUnknown('synthetic-settlement-operation')
            error.model_sent = True
            error.physical_request_id = 'synthetic-physical-one'
            error.observed_usage = body['usage']
            error.status = 200
            error.ledger_settlement = 'UNCONFIRMED'
            error.capture_state = 'COMMITTED'
            raise error

        self.client._post = unconfirmed
        with patch.dict(os.environ, {'KB_DEFER_DEFAULT_APP': '1'}):
            from kb_platform.api import create_app

            app = create_app(self.settings, profile=self.profile, models=self.models,
                storage_configuration=self.storage)
        headers = {'Authorization': 'Bearer ' + self.token}
        with TestClient(app) as client:
            scope = client.post('/scopes', headers=headers, json={'name': 'Synthetic settlement failure'}).json()['scope_id']
            path = f'/knowledge-bases/{scope}'
            uploaded = client.post(path + '/sources', headers=headers,
                files={'file': ('Original.md', b'Original rule value 42.', 'text/markdown')},
                data={'policy': json.dumps({'llm_context': True, 'allowed_providers': ['synthetic']})})
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            build = client.post(path + '/builds', headers=headers, json={'channels': ['bm25']})
            self.assertEqual(build.status_code, 201, build.text)
            self.assertEqual(client.post(path + f'/builds/{build.json()["build_id"]}/publish', headers=headers).status_code, 200)
            session = client.post(f'/scopes/{scope}/sessions', headers=headers, json={}).json()['session_id']
            failed = client.post(f'/agent/sessions/{session}/turns', headers=headers,
                json={'message': 'What is the original rule?', 'request_id': 'uncertain', 'expected_revision': 0})
            self.assertEqual(failed.status_code, 502, failed.text)
            self.assertEqual(failed.json()['operation_id'], 'synthetic-settlement-operation')
            recovered = client.get(f'/agent/sessions/{session}/requests/uncertain', headers=headers)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()['status'], 'reconciliation_required')
            self.assertEqual(recovered.json()['recovery_action'], 'inspect_operation')
            observed, = recovered.json()['observations']
            self.assertEqual(observed['operation_id'], failed.json()['operation_id'])
            self.assertEqual(observed['physical_request_id'], 'synthetic-physical-one')
            self.assertEqual(observed['observed_usage']['total_tokens'], 15)
            self.assertEqual(observed['provider_http_status'], 200)
            self.assertIs(observed['model_sent'], True)
            self.assertEqual(len(self.physical), 1)

    def test_document_authorization_revision_and_answer_via_same_http_application(self):
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {'KB_DEFER_DEFAULT_APP': '1'}):
            from kb_platform.api import create_app

            app = create_app(self.settings, profile=self.profile, models=self.models,
                storage_configuration=self.storage)
        headers = {'Authorization': 'Bearer ' + self.token}
        with TestClient(app) as client:
            self.assertEqual(client.get('/diagnostics').status_code, 401)
            diagnostic = client.get('/diagnostics', headers=headers)
            self.assertEqual(diagnostic.status_code, 200, diagnostic.text)
            self.assertEqual(diagnostic.json()['model']['provider_id'], 'synthetic')
            self.assertEqual(diagnostic.json()['configuration_sources']['models'], 'explicit')
            self.assertEqual(diagnostic.json()['model_calls'], 0)
            self.assertEqual(len(diagnostic.json()['application']['nodes']), 11)
            self.assertTrue(all(node for node in diagnostic.json()['application']['nodes']))
            scope = client.post('/scopes', headers=headers, json={'name': 'Synthetic API lifecycle'}).json()['scope_id']
            path = f'/knowledge-bases/{scope}'
            upload = client.post(path + '/sources', headers=headers,
                files={'file': ('Policy.md', b'Original rule value 42.', 'text/markdown')})
            self.assertEqual(upload.status_code, 201, upload.text)
            source = upload.json()['source_id']
            staged = client.post(path + '/builds', headers=headers, json={'channels': ['bm25']})
            self.assertEqual(staged.status_code, 201, staged.text)
            result = client.post(path + f'/builds/{staged.json()["build_id"]}/publish', headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            listing = client.get(path + '/documents', headers=headers)
            self.assertEqual(listing.status_code, 200, listing.text)
            current = listing.json()
            self.assertEqual(current['provider_id'], 'synthetic')
            self.assertFalse(current['documents'][0]['qa_ready'])
            self.assertTrue(current['documents'][0]['human_search_ready'])
            self.assertEqual(client.get(f'/scopes/{scope}/agent-sources', headers=headers).json(), [])
            allowed = client.put(f'/sources/{source}/policy', headers=headers, json={
                'policy': {'llm_context': True, 'allowed_providers': ['synthetic']},
                'expected_revision': current['authority_revision']})
            self.assertEqual(allowed.status_code, 200, allowed.text)
            stale = client.put(f'/sources/{source}/policy', headers=headers, json={
                'policy': {}, 'expected_revision': current['authority_revision']})
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(stale.json()['error'], 'AUTHORITY_REVISION_CONFLICT')
            ready = client.get(path + '/documents', headers=headers).json()
            self.assertTrue(ready['documents'][0]['qa_ready'])
            self.assertFalse(ready['documents'][0]['needs_publication'])
            reparsed = client.post(path + f'/sources/{source}/reparse', headers=headers,
                json={'expected_revision': ready['authority_revision']})
            self.assertEqual(reparsed.status_code, 200, reparsed.text)
            ready = client.get(path + '/documents', headers=headers).json()
            self.assertFalse(ready['documents'][0]['needs_publication'])
            session = client.post(f'/scopes/{scope}/sessions', headers=headers, json={})
            self.assertEqual(session.status_code, 201, session.text)
            session_id = session.json()['session_id']
            answer = client.post(f'/agent/sessions/{session_id}/turns', headers=headers,
                json={'message': 'What is the original rule?', 'request_id': 'api/lifecycle/1', 'expected_revision': 0})
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertEqual(answer.json()['status'], 'answered', answer.text)
            self.assertEqual(answer.json()['answer'], 'Original rule value 42.')
            recovered = client.get(f'/agent/sessions/{session_id}/requests/api%2Flifecycle%2F1', headers=headers)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()['response']['answer'], answer.json()['answer'])
            self.assertEqual(recovered.json()['observations'][0]['recovery_action'], 'none')
            self.assertEqual(recovered.json()['observations'][0]['observed_usage']['total_tokens'], 15)
            self.assertEqual(len(self.physical), 4)
            model_input = self.physical[2]['messages']
            self.assertIn('Original rule value 42.', json.dumps(model_input))
            citation = answer.json()['evidence'][0]['citation_id']
            original = client.get(path + f'/citations/{citation}/original', headers=headers)
            self.assertEqual(original.status_code, 200, original.text)
            self.assertEqual(original.content, b'Original rule value 42.')
            versions = client.get(path + f'/sources/{source}/versions', headers=headers)
            self.assertEqual(versions.status_code, 200, versions.text)
            self.assertEqual(len(versions.json()['versions']), 1)
            replacement = client.post(path + '/sources', headers=headers,
                files={'file': ('Policy.md', b'Original rule value 99.', 'text/markdown')},
                data={'source_id': source, 'expected_revision': str(ready['authority_revision'])})
            self.assertEqual(replacement.status_code, 201, replacement.text)
            current = client.get(path + '/documents', headers=headers).json()
            self.assertEqual(len(current['documents']), 1)
            self.assertTrue(current['documents'][0]['needs_publication'])
            search = client.post(path + '/search', headers=headers, json={'query': 'Original rule'})
            self.assertEqual(search.status_code, 200, search.text)
            self.assertTrue(search.json()['hits'])
            self.assertTrue(all('42' in hit['text'] for hit in search.json()['hits']))
            self.assertEqual(len(self.physical), 4)
            from .agent_graph_contracts import normal_script

            def ask(request_id, expected_text):
                self.cache.clear()
                self.script[:] = normal_script()
                session = client.post(f'/scopes/{scope}/sessions', headers=headers, json={})
                self.assertEqual(session.status_code, 201, session.text)
                result = client.post(f'/agent/sessions/{session.json()["session_id"]}/turns', headers=headers,
                    json={'message': 'What is the original rule?', 'request_id': request_id, 'expected_revision': 0})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['status'], 'answered', result.text)
                self.assertEqual(result.json()['answer'], expected_text)
                self.assertIn(expected_text, json.dumps(self.physical[-2]['messages']))
                self.assertTrue(result.json()['trace']['retrieval_stages'])
                return result.json()

            retained = ask('candidate-not-published', 'Original rule value 42.')
            self.assertEqual(retained['evidence'][0]['citation_id'], citation)
            self.assertEqual(len(self.physical), 8)
            partial = client.post(path + '/sources', headers=headers,
                files={'file': ('Incomplete.html', b'<p>Draft</p><img src="missing.png">', 'text/html')})
            self.assertEqual(partial.status_code, 201, partial.text)
            self.assertEqual(partial.json()['document']['status'], 'PARTIAL')
            retry = client.post(path + f'/sources/{partial.json()["source_id"]}/reparse', headers=headers, json={})
            self.assertEqual(retry.status_code, 200, retry.text)
            failed_build = client.post(path + '/builds', headers=headers, json={'channels': ['bm25']})
            self.assertEqual(failed_build.status_code, 409, failed_build.text)
            self.assertEqual(failed_build.json()['error'], 'INCOMPLETE_PARSE')
            listing = client.get(path + '/documents', headers=headers).json()
            document = next(item for item in listing['documents'] if item['source_id'] == partial.json()['source_id'])
            removed = client.delete(path + f'/bindings/{document["binding_id"]}', headers=headers,
                params={'expected_revision': listing['authority_revision']})
            self.assertEqual(removed.status_code, 200, removed.text)
            rebuilt = client.post(path + '/builds', headers=headers, json={'channels': ['bm25']})
            self.assertEqual(rebuilt.status_code, 201, rebuilt.text)
            published = client.post(path + f'/builds/{rebuilt.json()["build_id"]}/publish', headers=headers)
            self.assertEqual(published.status_code, 200, published.text)
            ask('candidate-now-published', 'Original rule value 99.')
            self.assertEqual(len(self.physical), 12)
            listing = client.get(path + '/documents', headers=headers).json()
            revoked = client.put(f'/sources/{source}/policy', headers=headers,
                json={'policy': {'llm_context': False, 'allowed_providers': ['synthetic']},
                    'expected_revision': listing['authority_revision']})
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertEqual(client.get(f'/scopes/{scope}/agent-sources', headers=headers).json(), [])
            redacted = client.get(f'/agent/sessions/{session_id}/requests/api%2Flifecycle%2F1', headers=headers)
            self.assertEqual(redacted.status_code, 200, redacted.text)
            self.assertEqual(redacted.json()['response']['status'], 'redacted')
            self.assertEqual(len(self.physical), 12)

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
