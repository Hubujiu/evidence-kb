import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from scripts.check_project import offline_transport_violations
from .offline_transport import offline_client_construction


_REAL_CLIENT = httpx.Client
_ROOT = Path(__file__).resolve().parents[2]
_FILES = ('offline_transport.py', 'embedding_jobs_contracts.py', 'model_review_contracts.py',
          'model_embedding_review_contracts.py', 'plugin_configuration_contracts.py')


class OfflineTransportContracts(unittest.TestCase):
    def test_construction_preserves_environment_and_real_client_without_network_transport(self):
        before = dict(os.environ)
        with patch('httpx._client.HTTPTransport', side_effect=AssertionError('Network transport constructed')):
            for trust_env in (True, False):
                with self.subTest(trust_env=trust_env):
                    with offline_client_construction():
                        client = httpx.Client(timeout=7, trust_env=trust_env)
                    with client:
                        self.assertIs(type(client), _REAL_CLIENT)
                        self.assertEqual(client.trust_env, trust_env)
                        self.assertEqual(client.timeout.read, 7)
                        with self.assertRaisesRegex(AssertionError, 'before its capture'):
                            client.get('https://fixture.invalid/unobserved')
                    self.assertTrue(client.is_closed)
        self.assertTrue(dict(os.environ) == before, 'Offline construction changed the process environment')
        self.assertIs(httpx.Client, _REAL_CLIENT)

    def test_exception_closes_all_constructed_clients_and_restores_factory(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture setup failed'):
            with offline_client_construction():
                first, second = httpx.Client(), httpx.Client()
                raise RuntimeError('fixture setup failed')
        self.assertTrue(first.is_closed)
        self.assertTrue(second.is_closed)
        self.assertIs(httpx.Client, _REAL_CLIENT)

    def test_explicit_routing_is_rejected_instead_of_silently_replaced(self):
        for key in ('transport', 'proxy', 'mounts'):
            with self.subTest(key=key), offline_client_construction():
                with self.assertRaisesRegex(ValueError, 'explicit transport or proxy'):
                    httpx.Client(**{key: None})

    def test_each_client_has_independent_fail_fast_transport(self):
        with offline_client_construction():
            first, second = httpx.Client(), httpx.Client()
        with first, second:
            self.assertIsNot(first._transport, second._transport)
            self.assertIsInstance(first._transport, httpx.MockTransport)
            self.assertIsInstance(second._transport, httpx.MockTransport)

    def test_capture_replaces_and_closes_placeholder_then_runs_real_metering(self):
        from .embedding_jobs_contracts import captured_google_fixture
        from .provider_capture import MeteredTransport

        created = []

        def capture_client(*args, **kwargs):
            client = _REAL_CLIENT(*args, **kwargs)
            created.append(client)
            return client

        with patch('kb_platform.rag_testing.offline_transport._REAL_CLIENT', new=capture_client), \
                patch('httpx._client.HTTPTransport', side_effect=AssertionError('Network transport constructed')):
            with captured_google_fixture('gemini_api') as fixture:
                self.assertEqual(len(created), 1)
                self.assertTrue(created[0].is_closed)
                self.assertIs(type(fixture.provider.client), _REAL_CLIENT)
                self.assertIsInstance(fixture.provider.client._transport, MeteredTransport)
                vectors = fixture.provider.documents(['synthetic source'])
                self.assertEqual(len(vectors), 1)
                self.assertEqual(len(vectors[0]), 768)
                self.assertEqual(len(fixture.sent), 1)
                self.assertEqual(fixture.ledger.summary()['pending_or_unknown'], 0)
                self.assertEqual(fixture.ledger.summary()['effective_usage_or_reserved']['remote_embedding_calls'], 1)
            self.assertTrue(fixture.provider.client.is_closed)

    def test_live_sources_satisfy_mandatory_offline_construction_policy(self):
        self.assertEqual(offline_transport_violations(_ROOT), [])

    def test_policy_rejects_constructor_and_wiring_regressions(self):
        changes = (
            ('offline_transport.py', 'transport=httpx.MockTransport(reject_uncaptured_send)', 'transport=None'),
            ('offline_transport.py', '*args, **kwargs, transport=', '*args, trust_env=False, **kwargs, transport='),
            ('offline_transport.py', '_REAL_CLIENT(*args', 'httpx.Client(*args'),
            ('offline_transport.py', "raise AssertionError('Offline fixture sent before its capture transport was installed')",
             'return httpx.Response(200)'),
            ('offline_transport.py', 'client.close()', 'pass'),
            ('offline_transport.py', 'import httpx', 'import httpx\nimport os'),
            ('offline_transport.py', '    clients = []', "    __import__('os').environ.clear()\n    clients = []"),
            ('offline_transport.py', 'return client', 'return unittest.mock.MagicMock()'),
            ('embedding_jobs_contracts.py', 'with offline_client_construction():', 'with closing(None):'),
            ('model_review_contracts.py', 'with offline_client_construction():', 'with nullcontext():'),
            ('model_embedding_review_contracts.py', 'with offline_client_construction():', 'with closing(None):'),
            ('plugin_configuration_contracts.py', 'with offline_client_construction():', 'with TemporaryDirectory():'),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'kb_platform/rag_testing'
            destination.mkdir(parents=True)
            originals = {name: (_ROOT / 'kb_platform/rag_testing' / name).read_text('utf-8') for name in _FILES}
            for name, source in originals.items():
                (destination / name).write_text(source, 'utf-8')
            self.assertEqual(offline_transport_violations(root), [])
            for name, before, after in changes:
                with self.subTest(file=name, mutation=after):
                    self.assertIn(before, originals[name])
                    (destination / name).write_text(originals[name].replace(before, after, 1), 'utf-8')
                    self.assertTrue(offline_transport_violations(root))
                    (destination / name).write_text(originals[name], 'utf-8')
