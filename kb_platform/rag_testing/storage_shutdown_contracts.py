from pathlib import Path
from threading import RLock
import unittest
from unittest.mock import Mock, patch

from kb_platform.application_storage import ApplicationStorage
from kb_platform.storage_adapters.redis_cache import RedisCacheProvider
from kb_platform.storage_sdk import StorageError
from .storage_shutdown_lint import APPLICATION, PROVIDER, storage_shutdown_violations


ROOT = Path(__file__).resolve().parents[2]


def application_with(provider):
    application = ApplicationStorage.__new__(ApplicationStorage)
    application._lock, application._active, application._closing = RLock(), 0, False
    application._generic, application._structured, application._objects = {}, {}, {}
    application._instances, application._bound = [provider], {}
    application._registry = Mock()
    return application


class StorageShutdownContracts(unittest.TestCase):
    def test_application_retains_actual_redis_provider_after_failed_physical_close(self):
        client = Mock()
        client.close.side_effect = [RuntimeError('physical close failed'), None]
        with patch('kb_platform.storage_adapters.redis_cache.redis.Redis.from_url', return_value=client):
            provider = RedisCacheProvider('redis://localhost:6379/0')
        application = application_with(provider)
        with self.assertRaisesRegex(RuntimeError, 'physical close failed'):
            application.close()
        self.assertEqual(application._instances, [provider])
        with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_UNAVAILABLE'):
            with application._operation():
                self.fail('closing application admitted work')
        with self.assertRaisesRegex(StorageError, 'STORAGE_PROVIDER_CLOSED'):
            with provider._connection():
                self.fail('closing provider admitted work')
        application.close()
        application.close()
        self.assertEqual(client.close.call_count, 2)
        self.assertEqual(application._instances, [])

    def test_application_busy_close_drains_active_operation_before_cleanup(self):
        provider = Mock()
        application = application_with(provider)
        with application._operation():
            with self.assertRaisesRegex(StorageError, 'STORAGE_SERVICE_BUSY'):
                application.close()
            provider.close.assert_not_called()
        application.close()
        provider.close.assert_called_once()

    def test_endpoint_failure_preserves_retry_order_and_provider(self):
        provider, endpoint = Mock(), Mock()
        endpoint.close.side_effect = [RuntimeError('endpoint failed'), None]
        application = application_with(provider)
        application._structured['first'] = endpoint
        with self.assertRaisesRegex(RuntimeError, 'endpoint failed'):
            application.close()
        self.assertIs(application._structured['first'], endpoint)
        provider.close.assert_not_called()
        application.close()
        self.assertEqual(endpoint.close.call_count, 2)
        provider.close.assert_called_once()

    def test_reviewed_shutdown_boundary_is_enforced(self):
        self.assertEqual(storage_shutdown_violations(
            (ROOT / PROVIDER).read_text('utf-8'), (ROOT / APPLICATION).read_text('utf-8')), [])

    def test_semantic_mutants_fail_even_without_hash_checks(self):
        provider = (ROOT / PROVIDER).read_text('utf-8')
        application = (ROOT / APPLICATION).read_text('utf-8')
        mutations = [
            (provider.replace('self._client.close()\n                    self._closed = True',
                              'self._closed = True\n                    self._client.close()'), application),
            (provider.replace('if self._closing:', 'if self._closed:'), application),
            (provider.replace('if self._active:', 'if False:'), application),
            (provider.replace('self._active -= 1', 'self._active = 0'), application),
            (provider, application.replace('self._instances[-1].close()\n                self._instances.pop()',
                                           'self._instances.pop().close()')),
            (provider, application.replace('endpoint.close()\n                    del endpoints[name]',
                                           'del endpoints[name]\n                    endpoint.close()')),
            (provider, application.replace('if self._active:', 'if False:')),
            (provider, application.replace('self._active -= 1', 'self._active = 0')),
        ]
        for changed_provider, changed_application in mutations:
            with self.subTest(provider=changed_provider != provider):
                self.assertNotEqual((changed_provider, changed_application), (provider, application))
                self.assertTrue(storage_shutdown_violations(changed_provider, changed_application))
