from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from kb_platform.blob_service import BlobService
from kb_platform.database_contracts import CommitUnknown
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from kb_platform.model_http import AnthropicAdapter, OpenAICompatibleAdapter
from kb_platform.model_sdk import Capability, ModelRequest
from kb_platform.persistence_local import local_database
from kb_platform.provider_admission import CaptureAdmissionError, ProviderAdmission
from kb_platform.storage import FileBlobProvider

from .budget import BudgetLedger
from .contracts import digest
from .formal_state_machines import ROOT, registry_violations, source_digest
from .provider_admission_rules import provider_admission_violations


class ProviderAdmissionFormalContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.serial = 0
        self.request = ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'synthetic'}]})

    def adapter(self, adapter_type=OpenAICompatibleAdapter):
        self.serial += 1
        root = self.root / str(self.serial)
        root.mkdir()
        ledger = BudgetLedger(root / 'budget.sqlite3', {'requests': 3})
        self.addCleanup(ledger.close)
        sent = []

        def respond(request):
            sent.append(request)
            return httpx.Response(403, json={'error': 'synthetic denied'})

        client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        self.addCleanup(client.close)
        options = dict(provider='fixture', model='fixed', endpoint='https://fixture.invalid/v1',
            api_key='synthetic', capabilities=[Capability.CHAT], ledger=ledger,
            reservation={'requests': 1}, client=client)
        return SimpleNamespace(root=root, ledger=ledger, sent=sent, options=options,
                               adapter=adapter_type(**options), adapter_type=adapter_type)

    def admission(self, *, journal=False):
        self.serial += 1
        root = self.root / str(self.serial)
        provider = SimpleNamespace(provider_id='fixture', model='fixed',
                                   base_url='https://fixture.invalid', manifest={})
        if not journal:
            admission = ProviderAdmission(provider, root)
            return admission, lambda: ProviderAdmission.from_directory(admission.directory)
        blobs = BlobService()
        blobs.register('local', FileBlobProvider(root / 'objects'))
        self.addCleanup(blobs.close)
        objects = blobs.bind('local', space='admission-contract')
        def factory():
            database, owned = local_database(root / 'admission.sqlite3', owner='provider-admission')
            self.addCleanup(owned.close)
            return ProviderAdmission(provider, database=database, objects=objects)
        return factory(), factory

    def block(self, admission, number):
        admission.block(str(number) * 32, payload_sha256=str(number) * 64, category='synthetic')

    def test_403_is_durable_before_lost_settlement_acknowledgement_for_both_adapters(self):
        for adapter_type in (OpenAICompatibleAdapter, AnthropicAdapter):
            with self.subTest(adapter=adapter_type.__name__):
                state = self.adapter(adapter_type)
                finish = state.ledger.finish
                unknown = CommitUnknown('settlement-commit-control')
                cause = OSError('synthetic acknowledgement lost after SQLite commit')

                def lose_acknowledgement(*args, **kwargs):
                    finish(*args, **kwargs)
                    raise unknown from cause

                with patch.object(state.ledger, 'finish', side_effect=lose_acknowledgement) as called:
                    with self.assertRaises(RemoteModelError) as raised:
                        state.adapter.invoke(self.request)
                error = raised.exception
                self.assertEqual(called.call_count, 1)
                self.assertEqual(error.code, 'PROVIDER_LEDGER_ERROR')
                self.assertIs(error.__cause__, unknown)
                self.assertIs(unknown.__cause__, cause)
                self.assertEqual(unknown.operation_id, 'settlement-commit-control')
                self.assertIs(error.model_sent, True)
                physical = state.ledger.entry(error.physical_request_id)
                self.assertEqual(physical['status'], 'ERROR')
                self.assertEqual(physical['usage'], {'requests': 1})
                self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
                for adapter in (state.adapter, adapter_type(**state.options)):
                    self.assertEqual(adapter.admission.snapshot()['state'], 'BLOCKED')
                    with self.assertRaises(CaptureAdmissionError) as blocked:
                        adapter.invoke(self.request)
                    self.assertIs(blocked.exception.model_sent, False)
                self.assertEqual(len(state.sent), 1)
                self.assertEqual(state.ledger.entry(error.physical_request_id), physical)
                self.assertEqual(state.ledger.summary()['physical_requests'], 1)

    def test_failed_403_event_write_keeps_pending_reservation_and_never_attempts_settlement(self):
        state = self.adapter()
        failure = OSError('synthetic admission write failure')
        with patch.object(state.adapter.admission, '_immutable', side_effect=failure), \
                patch.object(state.ledger, 'finish', wraps=state.ledger.finish) as finish:
            with self.assertRaises(RemoteModelError) as raised:
                state.adapter.invoke(self.request)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(finish.call_count, 0)
        self.assertEqual(state.adapter.admission.snapshot()['state'], 'BLOCKED')
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 1)
        self.assertEqual(state.ledger.summary()['effective_usage_or_reserved'], {'requests': 1})
        with self.assertRaises(CaptureAdmissionError):
            state.adapter.invoke(self.request)
        reopened = OpenAICompatibleAdapter(**state.options)
        with self.assertRaises(ModelPreflightError) as blocked:
            reopened.invoke(self.request)
        self.assertEqual(blocked.exception.code, 'UNRESOLVED_PHYSICAL_REQUEST')
        self.assertEqual(len(state.sent), 1)
        self.assertEqual(state.ledger.summary()['physical_requests'], 1)

    def test_failed_post_settlement_audit_write_cannot_drop_durable_403(self):
        state = self.adapter()
        replace = Path.replace
        failure = OSError('synthetic capture acknowledgement lost')

        def fail_after_send(source, target):
            if state.sent and source.parent == state.adapter.audit_root:
                raise failure
            return replace(source, target)

        with patch.object(Path, 'replace', fail_after_send):
            with self.assertRaises(RemoteModelError) as raised:
                state.adapter.invoke(self.request)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(state.ledger.summary()['pending_or_unknown'], 0)
        with self.assertRaises(CaptureAdmissionError):
            OpenAICompatibleAdapter(**state.options).invoke(self.request)
        self.assertEqual(len(state.sent), 1)

    def test_ephemeral_denial_changes_checkpoint_and_cannot_create_orphan_ack(self):
        for journal in (False, True):
            with self.subTest(journal=journal):
                admission, reopen = self.admission(journal=journal)
                self.block(admission, 1)
                reviewed = admission.snapshot()
                original = admission._items('events')
                with patch.object(admission, '_immutable', side_effect=OSError('synthetic event unavailable')):
                    with self.assertRaises(OSError):
                        self.block(admission, 2)
                current = admission.snapshot()
                self.assertNotEqual(current['checkpoint'], reviewed['checkpoint'])
                self.assertEqual(current['unpersisted_error_ids'], ['2' * 32])
                with self.assertRaisesRegex(ValueError, 'CHECKPOINT_CHANGED'):
                    admission.acknowledge(expected_checkpoint=reviewed['checkpoint'], evidence_reference='review E1')
                with self.assertRaisesRegex(ValueError, 'RECEIPT_UNPERSISTED'):
                    admission.acknowledge(expected_checkpoint=current['checkpoint'], evidence_reference='review E1 E2')
                self.assertEqual(admission._items('acknowledgements'), [])
                self.assertEqual(admission._items('events'), original)
                self.assertEqual(admission.snapshot()['state'], 'BLOCKED')
                self.assertEqual(reopen().snapshot()['state'], 'BLOCKED')
                capture = self.root / ('capture-' + str(self.serial))
                capture.mkdir()
                payload = {'synthetic': 'event 2'}
                (capture / 'event.json').write_text(json.dumps({'physical_request_id': '2' * 32,
                    'http_status': 403, 'status': 'ERROR', 'category': 'synthetic', 'payload': payload,
                    'payload_sha256': digest(payload), 'provider_scope': admission.scope_hash}), 'utf-8')
                admission.seed_from_capture(capture)
                receipt = admission.acknowledge(expected_checkpoint=admission.snapshot()['checkpoint'],
                                               evidence_reference='external repair of both errors')
                self.assertEqual(receipt['observed_error_ids'], ['1' * 32, '2' * 32])
                reopen().require_available()

    def test_concurrent_new_denial_cannot_be_cleared_by_inflight_acknowledgement(self):
        for journal in (False, True):
            with self.subTest(journal=journal):
                admission, reopen = self.admission(journal=journal)
                self.block(admission, 1)
                original = admission._items('events')
                checkpoint = admission.snapshot()['checkpoint']
                immutable = admission._immutable

                def add_new_error_before_ack(key, value):
                    if key.startswith('acknowledgements/'):
                        self.block(admission, 2)
                    return immutable(key, value)

                with patch.object(admission, '_immutable', side_effect=add_new_error_before_ack):
                    receipt = admission.acknowledge(expected_checkpoint=checkpoint,
                                                   evidence_reference='external repair of E1')
                self.assertEqual(receipt['observed_error_ids'], ['1' * 32])
                self.assertIs(receipt['provider_probe_performed'], False)
                self.assertIs(receipt['budget_or_error_rows_modified'], False)
                self.assertEqual(admission._items('events')[0], original[0])
                for current in (admission, reopen()):
                    self.assertEqual(current.snapshot()['unacknowledged_error_ids'], ['2' * 32])
                    with self.assertRaises(CaptureAdmissionError):
                        current.require_available()

    def test_operator_ack_never_sends_or_changes_original_budget_or_error_receipt(self):
        state = self.adapter()
        with self.assertRaises(RemoteModelError) as raised:
            state.adapter.invoke(self.request)
        admission = state.adapter.admission
        checkpoint = admission.snapshot()['checkpoint']
        events = admission._items('events')
        budget = state.ledger.summary()
        physical = state.ledger.entry(raised.exception.physical_request_id)
        audit = {p.name: p.read_bytes() for p in state.adapter.audit_root.glob('*.json')}
        admission.acknowledge(expected_checkpoint=checkpoint, evidence_reference='synthetic external repair')
        self.assertEqual(len(state.sent), 1)
        self.assertEqual(state.ledger.summary(), budget)
        self.assertEqual(state.ledger.entry(raised.exception.physical_request_id), physical)
        self.assertEqual(admission._items('events'), events)
        self.assertEqual({p.name: p.read_bytes() for p in state.adapter.audit_root.glob('*.json')}, audit)
        ProviderAdmission.from_directory(admission.directory).require_available()

    def test_unavailable_or_orphan_ack_storage_fails_closed_without_send(self):
        state = self.adapter()
        admission = state.adapter.admission
        with patch.object(admission, '_read', side_effect=OSError('synthetic state unavailable')):
            with self.assertRaises(CaptureAdmissionError) as raised:
                state.adapter.invoke(self.request)
        self.assertIs(raised.exception.model_sent, False)
        self.block(admission, 1)
        event = admission.directory / 'events' / ('1' * 32 + '.json')
        admission.acknowledge(expected_checkpoint=admission.snapshot()['checkpoint'],
                              evidence_reference='synthetic external repair')
        event.unlink()
        with self.assertRaises(CaptureAdmissionError):
            state.adapter.invoke(self.request)
        with self.assertRaises(CaptureAdmissionError):
            ProviderAdmission.from_directory(admission.directory).require_available()
        self.assertEqual(state.sent, [])
        self.assertEqual(state.ledger.summary()['physical_requests'], 0)

    def test_acknowledgement_commit_ambiguity_preserves_append_only_receipt_without_retry(self):
        for journal in (False, True):
            with self.subTest(journal=journal):
                admission, reopen = self.admission(journal=journal)
                self.block(admission, 1)
                original = admission._items('events')
                immutable = admission._immutable
                unknown = CommitUnknown('operator-ack-commit-control')

                def lose_acknowledgement(key, value):
                    immutable(key, value)
                    raise unknown

                with patch.object(admission, '_immutable', side_effect=lose_acknowledgement) as append:
                    with self.assertRaises(CommitUnknown) as raised:
                        admission.acknowledge(expected_checkpoint=admission.snapshot()['checkpoint'],
                                              evidence_reference='synthetic external repair')
                self.assertIs(raised.exception, unknown)
                self.assertEqual(raised.exception.operation_id, 'operator-ack-commit-control')
                self.assertEqual(append.call_count, 1)
                self.assertEqual(len(admission._items('acknowledgements')), 1)
                self.assertEqual(admission._items('events'), original)
                recovered = reopen()
                recovered.require_available()
                with self.assertRaisesRegex(ValueError, 'NO_BLOCKED_PROVIDER'):
                    recovered.acknowledge(expected_checkpoint=recovered.snapshot()['checkpoint'],
                                          evidence_reference='must inspect rather than repeat')


class ProviderAdmissionLintContracts(unittest.TestCase):
    def test_permission_guards_survive_reviewed_hash_refresh(self):
        self.assertEqual(provider_admission_violations(ROOT), [])
        registry = json.loads((ROOT / 'formal/registry-v1.json').read_text('utf-8'))
        reviewed = next(row for row in registry['machines'] if row['id'] == 'provider_admission')
        block = ('            if response.status_code == 403:\n'
                 '                self.admission.block(identity, payload_sha256=payload_hash, category=category)\n')
        invoke = (ROOT / 'kb_platform/model_http.py').read_text('utf-8')
        late = invoke.replace(block, '').replace("                               diagnostic=f'HTTP_{response.status_code}')\n",
            "                               diagnostic=f'HTTP_{response.status_code}')\n" + block)
        swallowed = block.replace('                self.admission.block', '                try:\n                    self.admission.block') + \
            '                except OSError:\n                    pass\n'
        mutations = [
            ('kb_platform/model_http.py', invoke, late),
            ('kb_platform/model_http.py', block, swallowed),
            ('kb_platform/provider_admission.py', "digest({'files': files, 'observed_error_ids': sorted(events)})", 'digest(files)'),
            ('kb_platform/provider_admission.py', "if current['unpersisted_error_ids']:", 'if False:'),
            ('kb_platform/provider_admission.py', "            raise ValueError('PROVIDER_PERMISSION_RECEIPT_UNPERSISTED')",
             "            if False:\n                raise ValueError('PROVIDER_PERMISSION_RECEIPT_UNPERSISTED')"),
            ('kb_platform/provider_admission.py', 'not ids.issubset(persisted)', 'not ids.issubset(events)'),
            ('kb_platform/provider_admission.py', 'self._ephemeral.add(physical_id)', 'pass'),
            ('kb_platform/provider_admission.py', "'observed_error_ids': current['observed_error_ids']", "'observed_error_ids': self.snapshot()['observed_error_ids']"),
            ('kb_platform/provider_admission.py', "'provider_probe_performed': False", "'provider_probe_performed': True"),
            ('kb_platform/provider_admission.py', "blocked = self.snapshot()['state'] == 'BLOCKED'", 'blocked = False'),
            ('kb_platform/rag_testing/provider_capture.py', 'self.admission.block(identity, payload_sha256=', 'self.admission.require_available(identity, payload_sha256='),
        ]
        for relative, before, after in mutations:
            with self.subTest(path=relative, mutation=before[:60]), TemporaryDirectory() as directory:
                root = Path(directory)
                machine = deepcopy(reviewed)
                for path in machine['owners']:
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / path, target)
                target = root / relative
                original = target.read_text('utf-8')
                self.assertIn(before, original)
                target.write_text(original.replace(before, after), 'utf-8')
                for binding in machine['source_bindings']:
                    binding['sha256'] = source_digest(root, binding)
                for model in machine['models'] + machine['negative_controls']:
                    for key in ('module', 'config'):
                        target = root / model[key]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(ROOT / model[key], target)
                document = {'schema_version': 1, 'machines': [machine],
                            'discovery': {'roots': ['kb_platform'], 'exclusions': []}}
                errors = registry_violations(root, document)
                self.assertFalse(any('source review drift' in error for error in errors), errors)
                self.assertTrue(any(error.startswith('provider_admission:') for error in errors), errors)
