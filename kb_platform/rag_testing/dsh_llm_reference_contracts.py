from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tomllib
import unittest

from kb_platform.plugin_native import PluginError, native_call


ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / 'config/plugin-runtime/dsh-llm-reference.json'
PROVENANCE = ROOT / 'config/plugin-runtime/dsh-llm-reference-provenance.json'


def workspace_dependencies():
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text('utf-8'))['workspace']
    manifests = {}
    for pattern in workspace['members']:
        for directory in ROOT.glob(pattern):
            document = tomllib.loads((directory / 'Cargo.toml').read_text('utf-8'))
            manifests[document['package']['name']] = (directory.resolve(), document)
    by_directory = {directory: name for name, (directory, _) in manifests.items()}

    def dependency_tables(document):
        for key, value in document.items():
            if key in {'dependencies', 'dev-dependencies', 'build-dependencies'}:
                yield value
            elif isinstance(value, dict):
                yield from dependency_tables(value)

    graph = {}
    for name, (directory, document) in manifests.items():
        local = set()
        for table in dependency_tables(document):
            for key, specification in table.items():
                specification = specification if isinstance(specification, dict) else {}
                target = specification.get('package', key)
                if 'path' in specification:
                    target = by_directory.get((directory / specification['path']).resolve(), target)
                if target in manifests:
                    local.add(target)
        graph[name] = local
    return graph


def reachable(graph, start):
    visited, pending = set(), list(graph[start])
    while pending:
        current = pending.pop()
        if current not in visited:
            visited.add(current)
            pending.extend(graph[current] - visited)
    return visited


class DshLlmReferenceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(REFERENCE.read_text('utf-8'))
        cls.cases = {case['id']: case for case in cls.fixture['cases']}

    def assert_reference_case(self, name):
        case = self.cases[name]
        execution = self.fixture['execution']
        state, response = None, None
        for event in case['events']:
            result = native_call(execution['command'], state=state, event=event,
                                 protocol=execution['protocol'],
                                 provider=execution['provider'], model=execution['model'])
            state, response = result['state'], result['response']
            if event['type'] != 'finish':
                self.assertIsNone(response, 'an unfinished provider stream was published as a response')
        if case.get('eof'):
            result = native_call(execution['command'], state=state, event=None,
                                 protocol=execution['protocol'],
                                 provider=execution['provider'], model=execution['model'])
            state, response = result['state'], result['response']
        expected = case.get('expected')
        if expected is not None:
            self.assertIsNotNone(response)
            self.assertEqual(response['provider'], execution['provider'])
            self.assertEqual(response['model'], execution['model'])
            self.assertEqual(response['content'], expected['content'])
            self.assertEqual(response['finish']['kind'], expected['finish'])
            self.assertEqual(response['usage'], expected['usage'])
            if 'failure_code' in expected:
                self.assertEqual(response['finish']['failure']['code'], expected['failure_code'])
            if 'replayState' in expected:
                self.assertEqual(response['replayState'], expected['replayState'])
        for rejected in case.get('rejected_events', []):
            original = deepcopy(state)
            with self.assertRaises(PluginError) as raised:
                native_call(execution['command'], state=state, event=rejected['event'],
                            protocol=execution['protocol'],
                            provider=execution['provider'], model=execution['model'])
            self.assertEqual(raised.exception.code, rejected['code'])
            self.assertEqual(state, original)

    def test_fixture_provenance_and_all_cases_are_used(self):
        provenance = json.loads(PROVENANCE.read_text('utf-8'))
        self.assertEqual(provenance['fixture_text_sha256'],
                         sha256(REFERENCE.read_text('utf-8').encode()).hexdigest())
        self.assertEqual(self.fixture['source']['commit'], provenance['commit'])
        self.assertEqual(provenance['commit'], 'ce7073d51254c26cd63b22a60835b8f948deb447')
        self.assertEqual(self.fixture['source']['kind'], 'synthetic_semantic_cases')
        cases = {method.removeprefix('test_reference_') for method in dir(self)
                 if method.startswith('test_reference_')}
        self.assertEqual(set(self.cases), cases)
        self.assertEqual(len(provenance['upstream_sources']), 4)
        for source in provenance['upstream_sources']:
            self.assertEqual(len(source['git_blob_sha']), 40)
            self.assertIn(provenance['commit'], source['url'])

    def test_reference_usage_snapshot_replaces_previous(self):
        self.assert_reference_case('usage_snapshot_replaces_previous')

    def test_reference_tool_arguments_remain_raw_json(self):
        self.assert_reference_case('tool_arguments_remain_raw_json')

    def test_reference_missing_usage_remains_unknown(self):
        self.assert_reference_case('missing_usage_remains_unknown')

    def test_reference_max_tokens_removes_tool_and_replay_entry(self):
        self.assert_reference_case('max_tokens_removes_tool_and_replay_entry')

    def test_reference_usage_after_finish_is_rejected(self):
        self.assert_reference_case('usage_after_finish_is_rejected')

    def test_reference_duplicate_finish_is_rejected(self):
        self.assert_reference_case('duplicate_finish_is_rejected')

    def test_reference_eof_without_finish_retains_observations(self):
        self.assert_reference_case('eof_without_finish_retains_observations')

    def test_reference_partial_usage_cannot_be_reported_as_known(self):
        self.assert_reference_case('partial_usage_cannot_be_reported_as_known')

    def test_native_protocol_version_must_be_explicit_and_supported(self):
        for fields in ({}, {'protocol': 'llm-service/2'}):
            with self.subTest(fields=fields), self.assertRaises(PluginError) as raised:
                native_call('llm_stream_apply', state=None, event=None,
                            provider='fixture', model='reference', **fields)
            self.assertEqual(raised.exception.code, 'LLM_PROTOCOL_UNSUPPORTED')

    def test_unknown_request_fields_are_rejected_before_dispatch(self):
        request = {'provider': 'fixture', 'model': 'reference', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}]}
        capabilities = {'streaming': True, 'tools': True, 'options': []}
        for field, value in (('tool_choice', 'auto'), ('top_p', 0.5)):
            with self.subTest(field=field), self.assertRaises(PluginError) as raised:
                native_call('llm_validate_request', protocol='llm-service/1',
                            request={**request, field: value}, capabilities=capabilities)
            self.assertEqual(raised.exception.code, 'INVALID_LLM_REQUEST')

    def test_tool_history_requires_explicit_provider_support(self):
        request = {'provider': 'fixture', 'model': 'reference', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}],
            'toolHistory': {'tools': [], 'updates': []}}
        for tools, code in ((True, 'UNSUPPORTED_LLM_OPTION'), (False, 'UNSUPPORTED_LLM_TOOLS')):
            with self.subTest(tools=tools), self.assertRaises(PluginError) as raised:
                native_call('llm_validate_request', protocol='llm-service/1', request=request,
                            capabilities={'streaming': True, 'tools': tools, 'options': []})
            self.assertEqual(raised.exception.code, code)

    def test_tool_history_retains_declarations_and_references(self):
        tool = {'name': 'retrieve', 'description': 'Retrieve documents',
                'parameters': {'type': 'object'}, 'deferLoading': True}
        request = {'provider': 'fixture', 'model': 'reference', 'messages': [
            {'role': 'user', 'id': 'question', 'content': [{'type': 'text', 'text': 'hello'}]},
            {'role': 'developer', 'id': 'addition', 'content': [
                {'type': 'tool-addition', 'toolName': 'retrieve'}]}],
            'tools': [tool], 'toolHistory': {'tools': [], 'updates': [
                {'messageId': 'addition', 'additions': [tool]}]}}
        capabilities = {'streaming': True, 'tools': True, 'toolUpdate': 'addition-only', 'options': []}
        normalized = native_call('llm_validate_request', protocol='llm-service/1',
                                 request=request, capabilities=capabilities)['request']
        self.assertEqual(normalized['toolHistory'], request['toolHistory'])
        self.assertEqual(normalized['messages'], request['messages'])
        self.assertEqual(normalized['tools'], request['tools'])
        inline = deepcopy(request)
        inline['messages'][1]['content'][0]['tool'] = tool
        with self.assertRaises(PluginError) as raised:
            native_call('llm_validate_request', protocol='llm-service/1',
                        request=inline, capabilities=capabilities)
        self.assertEqual(raised.exception.code, 'INVALID_LLM_REQUEST')
        removal = deepcopy(request)
        removal['messages'].append({'role': 'developer', 'id': 'removal', 'content': [
            {'type': 'tool-removal', 'toolName': 'retrieve'}]})
        with self.assertRaises(PluginError) as raised:
            native_call('llm_validate_request', protocol='llm-service/1',
                        request=removal, capabilities=capabilities)
        self.assertEqual(raised.exception.code, 'UNSUPPORTED_LLM_CONTENT')
        normalized = native_call('llm_validate_request', protocol='llm-service/1', request=removal,
                                 capabilities={**capabilities, 'toolUpdate': 'in-history'})['request']
        self.assertEqual(normalized['messages'], removal['messages'])

    def test_replay_requires_a_nonempty_matching_registration_identity(self):
        replay = {'response': {'opaque': ['provider', 19]}, 'blocks': [{'opaque': 'block'}]}
        request = {'provider': 'fixture', 'model': 'reference', 'messages': [
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hello'}], 'source': {
                'provider': 'fixture', 'model': 'reference', 'replayState': replay}}]}
        capabilities = {'streaming': True, 'options': []}
        for supplied, current in ((None, None), ('', None), ('', ''), ('old', 'new')):
            with self.subTest(supplied=supplied, current=current):
                candidate = deepcopy(request)
                if supplied is not None:
                    candidate['messages'][0]['source']['adapterIdentity'] = supplied
                active = {} if current is None else {'adapterIdentity': current}
                normalized = native_call('llm_validate_request', protocol='llm-service/1',
                                         request=candidate, capabilities=capabilities, **active)['request']
                self.assertNotIn('replayState', normalized['messages'][0]['source'])
        request['messages'][0]['source']['adapterIdentity'] = 'service-1:registration-2'
        normalized = native_call('llm_validate_request', protocol='llm-service/1', request=request,
                                 capabilities=capabilities,
                                 adapterIdentity='service-1:registration-2')['request']
        self.assertEqual(normalized['messages'][0]['source']['replayState'], replay)

    def test_tool_history_validates_baseline_and_update_schemas(self):
        tool = {'name': 'retrieve', 'description': 'Retrieve documents', 'parameters': {'type': 'object'}}
        schemas = [[{**tool, 'name': ''}], [{**tool, 'parameters': []}],
                   [{**tool, 'deferLoading': False}], [tool, tool]]
        request = {'provider': 'fixture', 'model': 'reference', 'messages': [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}]}
        capabilities = {'streaming': True, 'tools': True, 'toolUpdate': 'in-history', 'options': []}
        for invalid in schemas:
            for history in ({'tools': invalid, 'updates': []}, {'tools': [], 'updates': [
                    {'messageId': 'declaration', 'additions': invalid}]}):
                with self.subTest(history=history), self.assertRaises(PluginError) as raised:
                    native_call('llm_validate_request', protocol='llm-service/1',
                                request={**request, 'toolHistory': history}, capabilities=capabilities)
                self.assertEqual(raised.exception.code, 'INVALID_LLM_TOOL_HISTORY')

    def test_restored_stream_state_rejects_unsupported_output_blocks(self):
        arguments = {'protocol': 'llm-service/1', 'provider': 'fixture', 'model': 'reference'}
        state = native_call('llm_stream_apply', state=None,
                            event={'type': 'block-start', 'index': 0, 'blockType': 'text'},
                            **arguments)['state']
        state['blocks'][0]['kind'] = 'image'
        with self.assertRaises(PluginError) as raised:
            native_call('llm_stream_apply', state=state, event=None, **arguments)
        self.assertEqual(raised.exception.code, 'INVALID_LLM_STREAM_STATE')

    def test_restored_stream_state_cannot_publish_an_incomplete_tool(self):
        arguments = {'protocol': 'llm-service/1', 'provider': 'fixture', 'model': 'reference'}
        state = native_call('llm_stream_apply', state=None, event={
            'type': 'tool-call-delta', 'index': 0, 'id': 'call-1', 'name': 'retrieve',
            'argumentsDelta': '{'}, **arguments)['state']
        state['finish'] = {'kind': 'tool-calls'}
        with self.assertRaises(PluginError) as raised:
            native_call('llm_stream_apply', state=state, event=None, **arguments)
        self.assertEqual(raised.exception.code, 'INVALID_LLM_STREAM_STATE')

    def test_lifecycle_core_has_no_transitive_model_storage_or_application_dependency(self):
        graph = workspace_dependencies()
        core = {'plugin-runtime', 'plugin-host'}
        self.assertTrue(core.issubset(graph))
        for package in core:
            with self.subTest(package=package):
                self.assertLessEqual(reachable(graph, package), core)

    def test_public_services_are_consumable_without_the_host_or_knowledge_application(self):
        graph = workspace_dependencies()
        for package in ('llm-service', 'storage-service'):
            with self.subTest(package=package):
                self.assertIn(package, graph)
                self.assertEqual(reachable(graph, package), set())
