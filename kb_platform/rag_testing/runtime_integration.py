import argparse
from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
import uvicorn

from .runner import write_json


def acceptance_dsn(dsn):
    selected = conninfo_to_dict(dsn)
    if (not selected.get('dbname', '').startswith('rust_plugin_acceptance_')
            or selected.get('host') != '127.0.0.1' or selected.get('port') != '55432'
            or selected.get('hostaddr', '127.0.0.1') != '127.0.0.1'
            or selected.get('user') != 'kb_app'):
        raise ValueError('Acceptance requires kb_app on a dedicated loopback acceptance database')
    return selected['dbname']


def acceptance_migration_dsn(dsn, migration_dsn):
    database = acceptance_dsn(dsn)
    selected = conninfo_to_dict(migration_dsn)
    if (selected.get('dbname') != database or selected.get('host') != '127.0.0.1'
            or selected.get('port') != '55432' or selected.get('hostaddr', '127.0.0.1') != '127.0.0.1'
            or selected.get('user') != 'kb_admin'):
        raise ValueError('Acceptance migrations require kb_admin on the same dedicated loopback database')
    return database


@contextmanager
def controlled_storage(dsn, migration_dsn, directory, observations):
    from sqlalchemy.engine import make_url

    from kb_platform.database_contracts import SpaceKey
    from kb_platform.storage_catalog import initialize_configuration
    from .storage_integration import StorageFixtures

    acceptance_migration_dsn(dsn, migration_dsn)
    credentials = {'KB_ACCEPTANCE_SQL_URL': dsn, 'KB_ACCEPTANCE_SQL_MIGRATION_URL': migration_dsn}
    with patch.dict(os.environ, credentials):
        fixtures = StorageFixtures({'postgresql': make_url(migration_dsn)}, directory, observations)
        application = 'runtime-acceptance-' + fixtures.run_id
        definition = {'version': 2, 'application_id': application, 'instances': {
            'sql': {'provider': 'postgresql', 'config': {'url_env': 'KB_ACCEPTANCE_SQL_URL',
                'migration_url_env': 'KB_ACCEPTANCE_SQL_MIGRATION_URL'}},
            'documents': {'provider': 'sqlite', 'config': {'root': str(fixtures.resource_root / 'documents')}},
            'originals': {'provider': 'files', 'config': {'root': str(fixtures.resource_root / 'originals')}},
        }, 'services': {
            'authority': {'instance': 'sql', 'export': 'structured'},
            'lexical': {'instance': 'sql', 'export': 'lexical'},
            'vector': {'instance': 'sql', 'export': 'vector'},
            'evidence': {'instance': 'documents', 'export': 'document'},
            'originals': {'instance': 'originals', 'export': 'object'},
        }, 'bindings': {
            'repository': {'primary': {'service': 'authority', 'owner': 'knowledge', 'space': 'content'}},
            'search': {name: {'service': service, 'owner': 'knowledge', 'space': name}
                       for name, service in (('bm25', 'lexical'), ('dense', 'vector'), ('source', 'evidence'))},
            'originals': {'objects': {'service': 'originals', 'owner': 'knowledge', 'space': 'originals'}},
        }}
        for consumer, slots in definition['bindings'].items():
            for binding in slots.values():
                if definition['services'][binding['service']]['instance'] == 'sql':
                    space = SpaceKey(application, binding['owner'], binding['space'])
                    fixtures.spaces.append(('postgresql', 'kb_' + space.digest[:24]))
        path = directory / 'storage.json'
        try:
            write_json(path, initialize_configuration(definition))
            observations['storage_configuration'] = {'version': 2, 'application_id': application,
                'authority_contract': 'storage.relational.structured/v1',
                'search_contracts': ['storage.lexical.bm25/v1', 'storage.vector/v1', 'storage.document/v1']}
            yield path, credentials
        finally:
            fixtures.close()


def build_suite(dsn, output, observations, *, migration_dsn=None):
    class RuntimeIntegration(unittest.TestCase):
        def setUp(self):
            from kb_platform.api import create_app
            from kb_platform.settings import Settings

            self.directory = output / self.id().split('.')[-1]
            self.directory.mkdir()
            self.tenant = 'runtime-integration-' + uuid4().hex
            self.token = 'owner-' + uuid4().hex
            self.denied_token = 'denied-' + uuid4().hex
            self.other_token = 'other-' + uuid4().hex
            identities = self.directory / 'identities.json'
            identities.write_text(json.dumps({
                self.token: {'tenant_id': self.tenant, 'principal_id': 'owner'},
                self.denied_token: {'tenant_id': self.tenant, 'principal_id': 'denied'},
                self.other_token: {'tenant_id': self.tenant + '-other', 'principal_id': 'owner'},
            }), encoding='utf-8')
            self.storage_context = controlled_storage(dsn, migration_dsn, self.directory, observations)
            storage_file, _ = self.storage_context.__enter__()
            self.addCleanup(self.storage_context.__exit__, None, None, None)
            settings = Settings(self.directory / 'originals', identities, storage_file)
            self.app = create_app(settings)
            self.host = self.app.state.root_plugins
            self.runtime = self.host.runtime_id
            self.started, self.server_errors = Event(), []
            started = self.started

            class Server(uvicorn.Server):
                async def startup(inner, sockets=None):
                    try:
                        await super().startup(sockets)
                    finally:
                        started.set()

            self.listener = socket.socket()
            self.listener.bind(('127.0.0.1', 0))
            self.listener.listen(128)
            self.port = self.listener.getsockname()[1]
            self.server = Server(uvicorn.Config(self.app, log_level='error', access_log=False,
                lifespan='on', timeout_graceful_shutdown=10))

            def run():
                try:
                    self.server.run(sockets=[self.listener])
                except BaseException as error:
                    self.server_errors.append(error)
                    self.started.set()

            self.worker = Thread(target=run, name='runtime-integration-http', daemon=True)
            self.worker.start()
            self.addCleanup(self.shutdown)
            self.assertTrue(self.started.wait(15), 'HTTP server did not signal startup')
            self.assertEqual(self.server_errors, [])
            self.assertTrue(self.server.started)
            self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.port}', timeout=20,
                trust_env=False, headers={'Authorization': 'Bearer ' + self.token})
            self.addCleanup(self.client.close)

        def shutdown(self):
            self.server.should_exit = True
            self.worker.join(15)
            self.assertFalse(self.worker.is_alive(), 'HTTP worker survived shutdown')
            self.assertEqual(self.server_errors, [])
            snapshot = self.host.inspect()
            self.assertEqual(snapshot['state'], 'disposed')
            self.assertEqual(snapshot['counts']['resources'], 0)
            self.assertEqual(snapshot['counts']['listeners'], 0)
            self.assertEqual(snapshot['counts']['leases'], 0)
            observations.setdefault('shutdown', []).append({'runtime': self.runtime,
                'state': snapshot['state'], 'counts': snapshot['counts'], 'worker_alive': False})

        def headers(self, response):
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertEqual(response.headers['referrer-policy'], 'no-referrer')

        def test_socket_authentication_and_response_middleware(self):
            response = self.client.get('/identity', headers={'Authorization': ''})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['www-authenticate'], 'Bearer')
            self.headers(response)
            response = self.client.get('/identity')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'tenant_id': self.tenant, 'principal_id': 'owner'})
            self.headers(response)
            response = self.client.get('/capabilities')
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()['remote_configured'])
            self.assertEqual(self.host.inspect()['runtime'], self.runtime)
            observations['http'] = {'transport': 'real loopback TCP HTTP via Uvicorn',
                'authentication': 'verified', 'response_headers': 'verified', 'model_routes': 0}

        def test_parser_subprocess_postgres_build_search_citation_and_acl(self):
            response = self.client.post('/knowledge-bases', json={'name': 'Runtime integration synthetic fixture'})
            self.assertEqual(response.status_code, 201, response.text)
            self.headers(response)
            kb_id = response.json()['kb_id']
            data = b'# Policy\n\nALPHA runtime integration evidence.\n\n' + (b'PADDING ' + b'x' * 1000 + b'\n\n') * 70
            self.assertGreater(len(data), 64 * 1024)
            original_run = subprocess.run
            with patch('kb_platform.document_parser.subprocess.run', wraps=original_run) as parser:
                response = self.client.post(f'/knowledge-bases/{kb_id}/sources',
                    headers={'Authorization': 'Bearer ' + self.denied_token},
                    files={'file': ('fixture.md', data, 'text/markdown')})
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(parser.call_count, 0)
                response = self.client.post(f'/knowledge-bases/{kb_id}/sources',
                    files={'file': ('fixture.md', data, 'text/markdown')})
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(parser.call_count, 1)
                self.assertEqual(parser.call_args.args[0][0], sys.executable)
                self.assertEqual(parser.call_args.args[0][1], '-c')
            source = response.json()
            document = source['document']
            self.assertEqual(document['original_sha256'], sha256(data).hexdigest())
            self.assertEqual(document['status'], 'READY')
            self.assertTrue(document['evidence_spans'])
            response = self.client.post(f'/knowledge-bases/{kb_id}/builds', json={'channels': ['bm25']})
            self.assertEqual(response.status_code, 201, response.text)
            build = response.json()
            self.assertEqual(build['status'], 'VALIDATED')
            response = self.client.post(f'/knowledge-bases/{kb_id}/builds/{build["build_id"]}/publish')
            self.assertEqual(response.status_code, 200, response.text)
            response = self.client.post(f'/knowledge-bases/{kb_id}/search', json={'query': 'ALPHA', 'channel': 'bm25'})
            self.assertEqual(response.status_code, 200, response.text)
            hits = response.json()['hits']
            self.assertTrue(hits)
            matching = next(hit for hit in hits if 'ALPHA' in hit['text'])
            citation = self.client.get(f'/knowledge-bases/{kb_id}/citations/{matching["citation_id"]}')
            self.assertEqual(citation.status_code, 200, citation.text)
            self.assertEqual(citation.json()['source_version_id'], sha256(data).hexdigest())
            self.assertEqual(citation.json()['parse_snapshot_id'], document['parse_snapshot_id'])
            self.assertTrue(citation.json()['anchors'])
            denied = self.client.post(f'/knowledge-bases/{kb_id}/search',
                headers={'Authorization': 'Bearer ' + self.denied_token}, json={'query': 'ALPHA'})
            self.assertEqual(denied.status_code, 403, denied.text)
            isolated = self.client.get('/knowledge-bases', headers={'Authorization': 'Bearer ' + self.other_token})
            self.assertEqual(isolated.status_code, 200, isolated.text)
            self.assertEqual(isolated.json(), [])
            snapshot = self.host.inspect()
            self.assertEqual(snapshot['runtime'], self.runtime)
            self.assertEqual(snapshot['counts']['operations'], 0)
            self.assertEqual(snapshot['counts']['leases'], 0)
            observations['business'] = {'nature': 'synthetic source, actual PostgreSQL/pg_textsearch',
                'kb_id': kb_id, 'source_id': source['source_id'], 'source_sha256': sha256(data).hexdigest(),
                'snapshot_id': document['parse_snapshot_id'], 'build_id': build['build_id'],
                'evidence_spans': len(document['evidence_spans']), 'matched_citation': matching['citation_id'],
                'parser_subprocesses': 1, 'denied_parser_subprocesses': 0, 'acl_denial_status': denied.status_code,
                'cross_tenant_rows': 0, 'runtime': self.runtime, 'new_remote_calls': 0}

    from .embedding_jobs_contracts import live_contract_suite as embedding_jobs
    from .model_review_contracts import live_contract_suite as model_review
    from .model_embedding_review_contracts import live_contract_suite as embedding_review

    return unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeIntegration),
                               embedding_jobs(dsn), model_review(dsn), embedding_review(dsn)))


def main():
    parser = argparse.ArgumentParser(description='Opt-in loopback HTTP, native parser subprocess, and isolated PostgreSQL integration; no provider calls.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dsn, migration_dsn = os.environ['KB_INTEGRATION_DSN'], os.environ['KB_INTEGRATION_MIGRATION_DSN']
    try:
        acceptance_migration_dsn(dsn, migration_dsn)
    except ValueError as error:
        parser.error(str(error))
    os.environ['PGHOSTADDR'] = '127.0.0.1'
    if os.environ.get('KB_REMOTE_MODELS') != '0' or os.environ.get('KB_DEFER_DEFAULT_APP') != '1':
        parser.error('Set KB_REMOTE_MODELS=0 and KB_DEFER_DEFAULT_APP=1')
    args.output.mkdir(parents=True, exist_ok=False)
    observations = {}
    with (args.output / 'checks.log').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            build_suite(dsn, args.output, observations, migration_dsn=migration_dsn))
    with psycopg.connect(dsn) as connection:
        database = connection.execute('SELECT current_database(), version()').fetchone()
        extensions = connection.execute("SELECT extname,extversion FROM pg_extension WHERE extname IN ('vector','pg_textsearch') ORDER BY extname").fetchall()
    from kb_platform.agent_native import library_path

    root = Path(__file__).resolve().parents[2]
    native = library_path().resolve()
    report = {'status': 'PASS' if result.wasSuccessful() else 'FAIL', 'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0,
        'scope': 'controlled actual loopback HTTP + PostgreSQL + native Python parsing subprocess; synthetic source; not production RAG quality',
        'database': database[0], 'postgresql': database[1], 'extensions': dict(extensions),
        'python': sys.version, 'libraries': {name: version(name) for name in ('uvicorn','fastapi','httpx','psycopg')},
        'native_sha256': sha256(native.read_bytes()).hexdigest(),
        'sql_sha256': {file.name: sha256(file.read_bytes()).hexdigest() for file in sorted((root / 'sql').glob('*.sql'))},
        'observations': observations}
    write_json(args.output / 'report.json', report)
    print(json.dumps({key: report[key] for key in ('status','tests','failures','errors','skipped','database')}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)


if __name__ == '__main__':
    main()
