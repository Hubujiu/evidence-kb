from copy import deepcopy
import json
from pathlib import Path
import sys

from kb_platform.application_composition import create_root_application
from kb_platform.application_knowledge import application_schemas
from kb_platform.knowledge_capabilities import CATALOG

from .contract_application_contracts import ContractApplicationContracts


def verify_context_contract_process(root):
    fixture = ContractApplicationContracts()
    composition = None
    try:
        fixture.setUp()
        replacement = deepcopy(next(item for item in CATALOG if item['implementation'] == 'knowledge.context'))
        replacement.update(implementation='acceptance.process.context', executor='process', command=[
            sys.executable, '-B', str(Path(__file__).resolve().parents[2] / 'examples/contract-context/worker.py')])
        fixture.profile['plugins']['context']['implementation'] = replacement['implementation']
        cwd = root / 'knowledge-process-context'
        cwd.mkdir()
        application_path, catalog_path = cwd / 'application.json', cwd / 'catalog.json'
        application_path.write_text(json.dumps(fixture.profile, ensure_ascii=False), 'utf-8')
        catalog_path.write_text(json.dumps({'version': 1, 'definitions': [replacement],
            'schemas': [{'implementation': replacement['implementation'], 'version': '1',
                         'schema': application_schemas()[('knowledge.context', '1')]}],
            'root': '.'}, ensure_ascii=False), 'utf-8')
        composition = create_root_application(fixture.settings, environment={
            'KB_APPLICATION_PROFILE': str(application_path), 'KB_PLUGIN_CATALOG': str(catalog_path)},
            models=fixture.models, storage_configuration=fixture.storage)
        application = composition.application
        plan = application.composition.plan
        transport = composition.host.service(plan['exports']['context']['context'])._implementation.transport
        fixture.assertEqual(transport.runtime_info['language'], 'python')
        fixture.assertEqual(len(plan['plugins']), 8)
        fixture.assertEqual(next(item for item in plan['plugins'] if item['id'] == 'context')['implementation'],
                            replacement['implementation'])
        scope, _ = fixture.publish(composition)
        hits = composition.platform.search(fixture.actor, scope, 'Original rule')['hits']
        fixture.assertTrue(hits)
        citation = hits[0]['citation_id']
        fixture.assertEqual(composition.platform.read_original(fixture.actor, scope, citation),
                            b'Original rule value 42.')
        fixture.assertTrue(application.sources(fixture.actor, scope))
        session = application.create(fixture.actor, scope)
        request = dict(message='What is the original rule?', request_id='process-context-request',
                       expected_revision=0, constraints=['Use only the original rule'])
        result = application.turn(fixture.actor, session['session_id'], **request)
        fixture.assertEqual(result['status'], 'answered', result)
        fixture.assertEqual(result['answer'], 'Original rule value 42.')
        fixture.assertIn(citation, {item['citation_id'] for item in result['evidence']})
        fixture.assertTrue(result['claims'])
        fixture.assertTrue(all(citation in claim['evidence_ids'] for claim in result['claims']))
        fixture.assertEqual(len(fixture.physical), 4)
        fixture.assertTrue(all('PROCESS_CONTEXT_PROTOCOL_ACCEPTANCE' in value['messages'][0]['content']
                               for value in fixture.physical))
        fixture.assertEqual(application.turn(fixture.actor, session['session_id'], **request), result)
        fixture.assertEqual(len(fixture.physical), 4)
        fixture.assertEqual(application.get(fixture.actor, session['session_id'])['revision'], 1)
        fixture.assertEqual(composition.host.inspect()['leases'], 0)
        composition.close()
        fixture.assertEqual(composition.host.inspect()['state'], 'disposed')
        fixture.assertIsNotNone(transport._process.poll())
        fixture.assertFalse(transport._reader.is_alive())
        fixture.assertFalse(transport._writer.is_alive())
        rows = [json.loads(path.read_text('utf-8')) for path in (cwd / '.plugin-journal/context').glob('*.json')]
        calls = [row for row in rows if row['request']['kind'] == 'invoke' and row['status'] == 'COMPLETE']
        callbacks = [row for row in rows if row['request']['kind'] == 'dependency_response'
                     and row['status'] == 'COMPLETE']
        fixture.assertTrue({'metadata', 'sources', 'prepare_request'} <= {row['request']['method'] for row in calls})
        fixture.assertEqual({row['request']['service'] for row in callbacks}, {'sessions', 'data', 'access'})
        fixture.assertTrue({('sessions', 'load'), ('sessions', 'events'), ('data', 'revalidate'),
                            ('access', 'authorize')} <= {
                                (row['request']['service'], row['request']['method']) for row in callbacks})
        fixture.assertFalse(any(row['status'] in {'ERROR', 'UNKNOWN'} for row in rows))
        fixture.assertEqual(sum(row['request']['kind'] == 'shutdown' and row['status'] == 'COMPLETE'
                                for row in rows), 1)
        return {'status': result['status'], 'answer': result['answer'], 'plugins': 8,
                'context': replacement['implementation'], 'model_calls': len(fixture.physical),
                'configured_via': ['KB_APPLICATION_PROFILE', 'KB_PLUGIN_CATALOG'],
                'external_model_calls': 0,
                'context_calls': len(calls), 'dependency_calls': len(callbacks),
                'dependency_aliases': ['access', 'data', 'sessions'],
                'source_citation': citation, 'state': composition.host.inspect()['state']}
    finally:
        if composition is not None:
            composition.close()
        fixture.doCleanups()
