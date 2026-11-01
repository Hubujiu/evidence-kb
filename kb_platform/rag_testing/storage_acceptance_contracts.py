from copy import deepcopy
from tempfile import TemporaryFile
import unittest

from kb_platform.host_protocol import PipeReader, encode_frame, read_frame
from .storage_integration import configuration


class StorageAcceptanceContracts(unittest.TestCase):
    def environment(self):
        return {
            'KB_STORAGE_TEST_POSTGRES_URL': 'postgresql://fixture:synthetic@127.0.0.1:55432/rust_plugin_acceptance_main',
            'KB_STORAGE_TEST_DENSE_POSTGRES_URL': 'postgresql://fixture:synthetic@127.0.0.1:55434/rust_plugin_acceptance_dense',
            'KB_STORAGE_TEST_MYSQL_URL': 'mysql://fixture:synthetic@127.0.0.1:55436/rust_plugin_acceptance_mysql',
            'KB_STORAGE_TEST_S3_ENDPOINT': 'http://127.0.0.1:59031',
            'KB_STORAGE_TEST_S3_ACCESS_KEY': 'synthetic',
            'KB_STORAGE_TEST_S3_SECRET_KEY': 'synthetic',
            'KB_STORAGE_TEST_REDIS_URL': 'redis://127.0.0.1:56379/0',
        }

    def test_actual_storage_acceptance_requires_explicit_isolated_loopback_resources(self):
        accepted = configuration(self.environment())
        self.assertEqual(accepted['postgresql'].database, 'rust_plugin_acceptance_main')
        for value in ('postgresql://fixture@localhost:55432/rust_plugin_acceptance_main',
                      'postgresql://fixture@127.0.0.1:55432/production',
                      'postgresql://fixture@127.0.0.1:55432/rust_plugin_acceptance_main?hostaddr=example.org',
                      'mysql://fixture@127.0.0.1:55432/rust_plugin_acceptance_main'):
            with self.subTest(value=value):
                environment = self.environment()
                environment['KB_STORAGE_TEST_POSTGRES_URL'] = value
                with self.assertRaises(ValueError):
                    configuration(environment)

    def test_missing_resource_or_shared_dense_database_never_silently_skips(self):
        environment = self.environment()
        for key in environment:
            with self.subTest(missing=key):
                selected = deepcopy(environment)
                del selected[key]
                with self.assertRaises(ValueError):
                    configuration(selected)
        environment['KB_STORAGE_TEST_DENSE_POSTGRES_URL'] = environment['KB_STORAGE_TEST_POSTGRES_URL']
        with self.assertRaisesRegex(ValueError, 'distinct actual databases'):
            configuration(environment)

    def test_pipe_reader_handles_regular_file_frames_and_eof(self):
        frame = {'protocol': 'plugin-host/1', 'kind': 'response', 'id': 1, 'ok': True,
                 'result': {'large': '证据' * 40000}}
        with TemporaryFile('w+b') as stream:
            stream.write(encode_frame(frame))
            stream.seek(0)
            reader = PipeReader(stream.fileno())
            self.assertEqual(read_frame(reader), frame)
            with self.assertRaises(EOFError):
                read_frame(reader)
