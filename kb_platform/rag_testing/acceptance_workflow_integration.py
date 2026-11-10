
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, Thread, current_thread
import unittest
from unittest.mock import patch

import httpx
import uvicorn

from . import delivery_http
from .delivery_fixtures import VERSION as FIXTURE_VERSION


class ScopeBoundary(BaseException):
    pass


class ScopeOnlyClient(httpx.Client):
    def __init__(self, *, observed=None, abrupt=False, **kwargs):
        super().__init__(**kwargs)
        self.observed, self.abrupt = observed, abrupt

    def request(self, method, url, **kwargs):
        if method != 'GET' and (method, url) != ('POST', '/knowledge-bases'):
            raise AssertionError('SCOPE_ONLY_FIXTURE_FORBIDS_FURTHER_EFFECTS')
        response = super().request(method, url, **kwargs)
        if method == 'POST':
            response.raise_for_status()
            if self.observed is not None:
                self.observed.append(response.json())
            if self.abrupt:

                os._exit(72)
            raise ScopeBoundary()
        return response


def attempt(configuration):
    options = json.loads(Path(configuration).read_text('utf-8'))
    try:
        with ScopeOnlyClient(base_url=options['url'], trust_env=False, timeout=10,
                headers={'Authorization': 'Bearer ' + options['token']}, abrupt=True) as client:
            delivery_http.prepare_http(client, options['fixtures'], options['output'], platform=None,
                expected_tenant_id=options['tenant'], physical_directory=options['captures'], channels=('bm25',))
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'code': getattr(error, 'code', str(error))}), flush=True)
        return 73
    raise AssertionError('SCOPE_ONLY_FIXTURE_MUST_NOT_FINISH_PREPARATION')


class AcceptanceWorkflowIntegration(unittest.TestCase):
    def setUp(self):
        from .contract_application_contracts import ContractApplicationContracts
        ContractApplicationContracts.setUp(self)

    def start_api(self):
        with patch.dict(os.environ, {'KB_DEFER_DEFAULT_APP': '1'}):
            from kb_platform.api import create_app
            app = create_app(self.settings, profile=self.profile, models=self.models,
                             storage_configuration=self.storage)
        ready, errors = Event(), []

        class Server(uvicorn.Server):
            async def startup(inner, sockets=None):
                try:
                    await super().startup(sockets)
                finally:
                    ready.set()

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(32)
        server = Server(uvicorn.Config(app, log_level='error', access_log=False,
            lifespan='on', timeout_graceful_shutdown=5))

        def serve():
            try:
                server.run(sockets=[listener])
            except BaseException as error:
                errors.append(error)
                ready.set()

        worker = Thread(target=serve, daemon=True, name='acceptance-api')
        worker.start()

        def close():
            server.should_exit = True
            worker.join(10)
            listener.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(close)
        self.assertTrue(ready.wait(15))
        self.assertTrue(server.started, errors)
        self.url = f'http://127.0.0.1:{listener.getsockname()[1]}'
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()

        (self.fixtures / 'fixtures.json').write_text(json.dumps({
            'schema_version': FIXTURE_VERSION, 'documents': [], 'cases': []}), 'utf-8')
        self.output, self.captures = self.root / 'preparation', self.root / 'captures'
        self.configuration = self.root / 'attempt.json'
        self.configuration.write_text(json.dumps({'url': self.url, 'token': self.token,
            'fixtures': str(self.fixtures), 'output': str(self.output), 'tenant': self.actor.tenant_id,
            'captures': str(self.captures)}), 'utf-8')

    def child(self):
        return subprocess.run([sys.executable, '-B', '-m', 'kb_platform.rag_testing.acceptance_workflow_integration',
            '--attempt', str(self.configuration)],
            capture_output=True, text=True, timeout=20, check=False)

    def scopes(self):
        with httpx.Client(base_url=self.url, trust_env=False, headers=self.headers) as client:
            return client.get('/knowledge-bases').raise_for_status().json()

    def test_two_process_preparers_cannot_create_two_scopes_for_one_operation(self):
        self.start_api()
        before_claim, release = Event(), Event()
        outcomes, observed = [], []
        atomic = delivery_http._atomic_json

        def gated(path, value):
            if (current_thread().name == 'first-preparer' and value.get('operations', {}).get(
                    'dev:scope', {}).get('status') == 'STARTED'):
                before_claim.set()
                if not release.wait(20):
                    raise AssertionError('PREPARER_BARRIER_TIMEOUT')
            return atomic(path, value)

        def first():
            try:
                with ScopeOnlyClient(base_url=self.url, trust_env=False, timeout=10,
                        headers=self.headers, observed=observed) as client:
                    delivery_http.prepare_http(client, self.fixtures, self.output, platform=None,
                        expected_tenant_id=self.actor.tenant_id, physical_directory=self.captures,
                        channels=('bm25',))
            except BaseException as error:
                outcomes.append(error)

        with patch.object(delivery_http, '_atomic_json', gated):
            producer = Thread(target=first, name='first-preparer')
            producer.start()
            try:
                self.assertTrue(before_claim.wait(15), outcomes)
                contender = self.child()
            finally:
                release.set()
                producer.join(15)
        self.assertFalse(producer.is_alive())
        rows = self.scopes()
        print(json.dumps({'scenario': 'concurrent-preparers', 'scope_count': len(rows),
            'contender_exit': contender.returncode, 'contender_output': contender.stdout,
            'scope_ids': [row['kb_id'] for row in rows]}), flush=True)
        self.assertEqual(len(rows), 1, 'One durable operation created multiple actual SQLite scopes')
        self.assertEqual(contender.returncode, 73, contender.stderr)
        self.assertEqual(json.loads(contender.stdout)['code'], 'DATABASE_GUARD_BUSY')
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], ScopeBoundary)
        state = json.loads((self.output / 'prepare-state.json').read_text('utf-8'))
        self.assertEqual(state['operations']['dev:scope']['status'], 'STARTED')
        self.assertEqual(self.physical, [])

        restarted = self.child()
        self.assertEqual(restarted.returncode, 73, restarted.stderr)
        self.assertIn('PREPARE_OPERATION_REQUIRES_RECONCILIATION', restarted.stdout)
        self.assertEqual(self.scopes(), rows)

    def test_process_death_after_scope_commit_does_not_replay_after_restart(self):
        self.start_api()
        crashed = self.child()
        self.assertEqual(crashed.returncode, 72, crashed.stderr)
        rows = self.scopes()
        self.assertEqual(len(rows), 1)
        raw = (self.output / 'prepare-state.json').read_bytes()
        self.assertEqual(json.loads(raw)['operations']['dev:scope']['status'], 'STARTED')
        restarted = self.child()
        self.assertEqual(restarted.returncode, 73, restarted.stderr)
        self.assertIn('PREPARE_OPERATION_REQUIRES_RECONCILIATION', restarted.stdout)
        self.assertEqual((self.output / 'prepare-state.json').read_bytes(), raw)
        self.assertEqual(self.scopes(), rows)
        for terminal in ('UNKNOWN', 'ERROR'):

            state = json.loads(raw)
            state['operations']['dev:scope']['status'] = terminal
            delivery_http._atomic_json(self.output / 'prepare-state.json', state)
            frozen = (self.output / 'prepare-state.json').read_bytes()
            rejected = self.child()
            self.assertEqual(rejected.returncode, 73, rejected.stderr)
            self.assertIn('PREPARE_OPERATION_REQUIRES_RECONCILIATION', rejected.stdout)
            self.assertEqual((self.output / 'prepare-state.json').read_bytes(), frozen)
            self.assertEqual(self.scopes(), rows)
        self.assertEqual(self.physical, [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--attempt', type=Path)
    args = parser.parse_args()
    if args.attempt is not None:
        return attempt(args.attempt)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AcceptanceWorkflowIntegration)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun == 2 and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(main())
