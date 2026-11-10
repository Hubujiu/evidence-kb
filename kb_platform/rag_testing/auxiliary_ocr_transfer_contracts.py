import base64
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.knowledge_model_ports import AuxiliaryModelEndpoint, METHODS
from kb_platform.ocr_contract import OCR_CONTRACT_VERSION, validate_ocr_response
from kb_platform.plugin_contracts import ContractError, ServiceEndpoint
from kb_platform.plugin_host import PluginHost, PluginSpec
from kb_platform.plugin_native import PluginError


SYNTHETIC_RESULT = {'blocks': [{'text': 'Synthetic OCR fixture', 'bbox': [0, 0, 1, 1]}],
                    'complete': True, 'unreadable': []}


class AuxiliaryOCRTransferContracts(unittest.TestCase):
    def fixture(self, action=None):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image = Path(directory.name) / 'synthetic-image.bin'
        image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic-two-part-image')
        data = image.read_bytes()
        provider = SimpleNamespace(complete_json=Mock(side_effect=action) if action is not None
            else Mock(return_value=deepcopy(SYNTHETIC_RESULT)))
        endpoint = AuxiliaryModelEndpoint(ocr=provider)
        self.addCleanup(endpoint.close)
        return SimpleNamespace(endpoint=endpoint, wire=ServiceEndpoint(endpoint, METHODS), provider=provider,
            data=data, request={'system': 'Synthetic system', 'user': 'Synthetic user', 'purpose': 'ocr',
                'image_size': len(data), 'image_sha256': sha256(data).hexdigest(),
                'contract_version': OCR_CONTRACT_VERSION})

    def append(self, wire, token, offset, data):
        return wire.ocr_append({**token, 'offset': offset,
            'data_base64': base64.b64encode(data).decode('ascii')})

    def complete_upload(self, case, wire=None):
        wire = case.wire if wire is None else wire
        token = wire.ocr_begin(case.request)
        split = len(case.data) // 2
        self.assertEqual(self.append(wire, token, 0, case.data[:split]), {'offset': split})
        self.assertEqual(self.append(wire, token, split, case.data[split:]), {'offset': len(case.data)})
        return token

    def worker(self, action):
        outcomes, done = [], Event()

        def run():
            try:
                outcomes.append(('result', action()))
            except BaseException as error:
                outcomes.append(('error', error))
            finally:
                done.set()

        thread = Thread(target=run, daemon=True)
        thread.start()
        return SimpleNamespace(thread=thread, outcomes=outcomes, done=done)

    def finish_worker(self, worker):
        worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(worker.done.is_set())

    def test_two_opaque_uploads_keep_ordered_bytes_and_consume_independently(self):
        case = self.fixture()
        first = case.wire.ocr_begin(case.request)
        second_data = b'second-synthetic-image'
        second = case.wire.ocr_begin({**case.request, 'image_size': len(second_data),
            'image_sha256': sha256(second_data).hexdigest()})
        self.assertNotEqual(first, second)
        self.append(case.wire, first, 0, case.data[:8])
        self.append(case.wire, second, 0, second_data[:8])
        self.append(case.wire, first, 8, case.data[8:])
        self.append(case.wire, second, 8, second_data[8:])
        self.assertEqual(case.wire.ocr(second), SYNTHETIC_RESULT)
        self.assertEqual(case.wire.ocr(first), SYNTHETIC_RESULT)
        self.assertEqual([call.kwargs['images'] for call in case.provider.complete_json.call_args_list],
                         [[second_data], [case.data]])
        for call in case.provider.complete_json.call_args_list:
            self.assertIs(call.kwargs['validator'], validate_ocr_response)
            self.assertEqual(call.kwargs['contract_version'], OCR_CONTRACT_VERSION)
        self.assertEqual(case.endpoint._uploads, {})

    def test_capacity_rejects_ninth_upload_without_invocation_and_cancel_frees_one_slot(self):
        case = self.fixture()
        tokens = [case.wire.ocr_begin(case.request) for _ in range(8)]
        with self.assertRaises(ContractError) as failure:
            case.wire.ocr_begin(case.request)
        self.assertEqual(failure.exception.code, 'OCR_TRANSFERS_BUSY')
        self.assertEqual(len(case.endpoint._uploads), 8)
        self.assertTrue(case.wire.ocr_cancel(tokens[0])['cancelled'])
        replacement = case.wire.ocr_begin(case.request)
        self.assertNotIn(replacement, tokens)
        self.assertEqual(len(case.endpoint._uploads), 8)
        case.provider.complete_json.assert_not_called()

    def test_expiry_uses_strict_deadline_and_stale_token_cannot_consume_replacement(self):
        case = self.fixture()
        with patch('kb_platform.knowledge_model_ports.monotonic', return_value=10):
            tokens = [case.wire.ocr_begin(case.request) for _ in range(8)]
        with patch('kb_platform.knowledge_model_ports.monotonic', return_value=910):
            with self.assertRaises(ContractError) as failure:
                case.wire.ocr_begin(case.request)
            self.assertEqual(failure.exception.code, 'OCR_TRANSFERS_BUSY')
        with patch('kb_platform.knowledge_model_ports.monotonic', return_value=911):
            replacement = self.complete_upload(case)
            for token in tokens:
                with self.assertRaises(ContractError) as failure:
                    case.wire.ocr(token)
                self.assertEqual(failure.exception.code, 'OCR_TRANSFER_UNAVAILABLE')
                self.assertFalse(case.wire.ocr_cancel(token)['cancelled'])
            self.assertEqual(case.wire.ocr(replacement), SYNTHETIC_RESULT)
        self.assertEqual(case.provider.complete_json.call_count, 1)

    def test_rejected_append_keeps_previous_bytes_and_next_offset(self):
        case = self.fixture()
        token = case.wire.ocr_begin(case.request)
        self.append(case.wire, token, 0, case.data[:8])
        for offset, data in ((0, b'x'), (9, b'x'), (8, case.data)):
            with self.subTest(offset=offset, size=len(data)), self.assertRaises(ContractError) as failure:
                self.append(case.wire, token, offset, data)
            self.assertEqual(failure.exception.code, 'INVALID_OCR_TRANSFER')
        with self.assertRaises(ContractError) as failure:
            case.wire.ocr_append({**token, 'offset': 8, 'data_base64': '!not-base64!'})
        self.assertEqual(failure.exception.code, 'INVALID_OCR_TRANSFER')
        self.assertEqual(self.append(case.wire, token, 8, case.data[8:]), {'offset': len(case.data)})
        self.assertEqual(case.wire.ocr(token), SYNTHETIC_RESULT)
        self.assertEqual(case.provider.complete_json.call_args.kwargs['images'], [case.data])

    def test_incomplete_upload_is_not_consumed_and_complete_corrupt_upload_is_never_sent(self):
        case = self.fixture()
        token = case.wire.ocr_begin(case.request)
        self.append(case.wire, token, 0, case.data[:-1])
        with self.assertRaises(ContractError) as failure:
            case.wire.ocr(token)
        self.assertEqual(failure.exception.code, 'INVALID_OCR_TRANSFER')
        case.provider.complete_json.assert_not_called()
        self.append(case.wire, token, len(case.data) - 1, case.data[-1:])
        self.assertEqual(case.wire.ocr(token), SYNTHETIC_RESULT)
        corrupt = case.wire.ocr_begin(case.request)
        self.append(case.wire, corrupt, 0, b'x' + case.data[1:])
        with self.assertRaises(ContractError) as failure:
            case.wire.ocr(corrupt)
        self.assertEqual(failure.exception.code, 'INVALID_OCR_TRANSFER')
        self.assertEqual(case.provider.complete_json.call_count, 1)
        self.assertTrue(case.wire.ocr_cancel(corrupt)['cancelled'])

    def test_consumed_upload_frees_lock_and_cannot_be_replayed_while_provider_is_running(self):
        entered, release = Event(), Event()

        def complete(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic provider release timed out')
            return deepcopy(SYNTHETIC_RESULT)

        case = self.fixture(complete)
        token = self.complete_upload(case)
        first = self.worker(lambda: case.wire.ocr(token))
        rival = None
        try:
            self.assertTrue(entered.wait(5))

            def operate():
                with self.assertRaises(ContractError) as failure:
                    case.wire.ocr(token)
                self.assertEqual(failure.exception.code, 'OCR_TRANSFER_UNAVAILABLE')
                self.assertFalse(case.wire.ocr_cancel(token)['cancelled'])
                pending = case.wire.ocr_begin(case.request)
                self.assertTrue(case.wire.ocr_cancel(pending)['cancelled'])
                return 'single-consumer'

            rival = self.worker(operate)
            self.assertTrue(rival.done.wait(2))
            self.assertEqual(rival.outcomes, [('result', 'single-consumer')])
            self.assertEqual(case.provider.complete_json.call_count, 1)
        finally:
            release.set()
            self.finish_worker(first)
            if rival is not None:
                self.finish_worker(rival)
        self.assertEqual(first.outcomes, [('result', SYNTHETIC_RESULT)])
        self.assertEqual(case.endpoint._uploads, {})

    def test_provider_error_does_not_restore_consumed_upload_or_retry(self):
        error = OSError('Synthetic provider failure')
        case = self.fixture(lambda *args, **kwargs: (_ for _ in ()).throw(error))
        token = self.complete_upload(case)
        with self.assertRaises(ContractError) as failure:
            case.wire.ocr(token)
        self.assertEqual(failure.exception.code, 'CONTRACT_IMPLEMENTATION_FAILED')
        self.assertIs(failure.exception.__cause__, error)
        self.assertFalse(case.wire.ocr_cancel(token)['cancelled'])
        with self.assertRaises(ContractError) as replay:
            case.wire.ocr(token)
        self.assertEqual(replay.exception.code, 'OCR_TRANSFER_UNAVAILABLE')
        self.assertEqual(case.provider.complete_json.call_count, 1)

    def test_provider_base_exception_propagates_without_reinserting_consumed_upload(self):
        error = KeyboardInterrupt('Synthetic provider cancellation')
        case = self.fixture(lambda *args, **kwargs: (_ for _ in ()).throw(error))
        token = self.complete_upload(case)
        with self.assertRaises(KeyboardInterrupt) as failure:
            case.wire.ocr(token)
        self.assertIs(failure.exception, error)
        self.assertFalse(case.wire.ocr_cancel(token)['cancelled'])
        with self.assertRaises(ContractError) as replay:
            case.wire.ocr(token)
        self.assertEqual(replay.exception.code, 'OCR_TRANSFER_UNAVAILABLE')
        self.assertEqual(case.provider.complete_json.call_count, 1)

    def test_invalid_provider_result_is_terminal_for_the_consumed_upload(self):
        case = self.fixture(lambda *args, **kwargs: {'invalid': True})
        token = self.complete_upload(case)
        with self.assertRaises(ContractError) as failure:
            case.wire.ocr(token)
        self.assertEqual(failure.exception.code, 'INVALID_MODEL_OUTPUT')
        self.assertEqual(failure.exception.status_code, 502)
        self.assertEqual(failure.exception.details, {'kind': 'model'})
        self.assertFalse(case.wire.ocr_cancel(token)['cancelled'])
        self.assertEqual(case.provider.complete_json.call_count, 1)

    def test_direct_cleanup_discards_pending_upload_not_an_already_consumed_invocation(self):
        entered, release = Event(), Event()

        def complete(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic provider release timed out')
            return deepcopy(SYNTHETIC_RESULT)

        case = self.fixture(complete)
        token = self.complete_upload(case)
        pending = case.wire.ocr_begin(case.request)
        worker = self.worker(lambda: case.wire.ocr(token))
        try:
            self.assertTrue(entered.wait(5))
            case.endpoint.close()
            self.assertEqual(case.endpoint._uploads, {})
            self.assertFalse(case.wire.ocr_cancel(pending)['cancelled'])
            self.assertFalse(case.wire.ocr_cancel(token)['cancelled'])
        finally:
            release.set()
            self.finish_worker(worker)
        self.assertEqual(worker.outcomes, [('result', SYNTHETIC_RESULT)])
        self.assertEqual(case.provider.complete_json.call_count, 1)

    def test_host_drain_rejects_new_calls_and_defers_endpoint_cleanup_until_provider_returns(self):
        entered, release, disposed = Event(), Event(), []

        def complete(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Synthetic provider release timed out')
            return deepcopy(SYNTHETIC_RESULT)

        case = self.fixture(complete)

        def mount(context, config):
            context.provide('capabilities', case.wire)
            context.defer(lambda: (case.endpoint.close(), disposed.append(True)))

        host = PluginHost({'version': 1, 'plugins': [{'id': 'model', 'use': 'model'}]},
            [PluginSpec('model', ('capabilities',), (), mount)])
        host_wire = SimpleNamespace(
            ocr_begin=lambda request: host.call('capabilities', 'ocr_begin', request),
            ocr_append=lambda request: host.call('capabilities', 'ocr_append', request))
        token = self.complete_upload(case, host_wire)
        pending = host.call('capabilities', 'ocr_begin', case.request)
        worker = self.worker(lambda: host.call('capabilities', 'ocr', token))
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(PluginError) as failure:
                host.close()
            self.assertEqual(failure.exception.code, 'scope_busy')
            self.assertEqual(disposed, [])
            self.assertIn(pending['upload_id'], case.endpoint._uploads)
            with self.assertRaises(PluginError) as rejected:
                host.call('capabilities', 'ocr_begin', case.request)
            self.assertEqual(rejected.exception.code, 'scope_unavailable')
            self.assertEqual(case.provider.complete_json.call_count, 1)
        finally:
            release.set()
            self.finish_worker(worker)
            host.close()
        self.assertEqual(worker.outcomes, [('result', SYNTHETIC_RESULT)])
        self.assertEqual(disposed, [True])
        self.assertEqual(case.endpoint._uploads, {})
