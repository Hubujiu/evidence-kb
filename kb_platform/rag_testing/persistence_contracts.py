from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest

import httpx

from kb_platform.blob_service import BlobService
from kb_platform.database_contracts import SpaceKey
from kb_platform.database_providers import SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.embedding_jobs import recovered_observation
from kb_platform.persistence_migration import import_ledger, inspect_ledger_import
from kb_platform.provider_admission import CaptureAdmissionError, ProviderAdmission
from kb_platform.provider_runtime import RequestReceipts, RuntimePolicyError
from kb_platform.model_http import OpenAICompatibleAdapter
from kb_platform.model_service import Capability, ModelRequest
from kb_platform.storage import FileBlobProvider
from .budget import BudgetLedger
from .cache import StageCache, StageKey


def exercise_persistence(bind, objects, root):
    check = unittest.TestCase()
    key = StageKey(stage='retrieval', implementation='storage-contract/1', upstream=('question',),
        config_hash='config', corpus_hash='corpus', index_snapshot='index', principal='principal',
        acl_revision='acl-1', model_revision='model', sample_id='sample')
    first_cache = StageCache(database=bind('stage-cache', 'cache'), objects=objects)
    payload = {'evidence': '原文', 'source_sha256': 'immutable'}
    first_cache.put(key, payload)
    second_cache = StageCache(database=bind('stage-cache', 'cache'), objects=objects)
    check.assertEqual(second_cache.get(key, authorize=lambda: True), payload)
    with check.assertRaises(PermissionError):
        second_cache.get(key, authorize=lambda: False)
    with check.assertRaisesRegex(ValueError, 'stage output changed'):
        second_cache.put(key, {'evidence': 'different'})
    check.assertIsNone(second_cache.get(key.model_copy(update={'acl_revision': 'acl-2'}), authorize=lambda: True))
    check.assertIsNone(second_cache.get(key, authorize=lambda: True, mode='live', under_test=True))
    caps = {'calls': 8, 'tokens': 200}
    first = BudgetLedger(caps=caps, database=bind('physical-budget', 'budget'))
    second = BudgetLedger(caps=caps, database=bind('physical-budget', 'budget'))
    first.reserve('unknown', {'calls': 1, 'tokens': 40})
    first.finish('unknown', status='UNKNOWN')
    first.reserve('partial', {'calls': 1, 'tokens': 30})
    first.finish('partial', status='ERROR', usage={'calls': 1})
    check.assertEqual(second.entry('unknown'), {'id': 'unknown', 'status': 'UNKNOWN',
        'reserved': {'calls': 1, 'tokens': 40}, 'usage': None, 'category': 'runtime', 'diagnostic': None})
    check.assertEqual(second.entry('partial')['usage'], {'calls': 1})
    check.assertIsNone(second.entry('not-registered'))
    check.assertEqual(second.registered(['partial', 'missing', 'unknown', 'partial']), ['partial', 'unknown', 'partial'])
    check.assertEqual(second.registered([]), [])
    recovery_directory = Path(root) / 'embedding-captures'
    recovery_directory.mkdir(parents=True, exist_ok=True)
    recovery_manifest, recovery_context = {'model': 'frozen'}, {'acl': 'current'}
    recovery_record = {'status': 'ERROR', 'request_id': 'partial', 'physical_request_id': 'partial',
        'task': 'document', 'texts': ['original'], 'provider_manifest': recovery_manifest,
        'audit_context': recovery_context, 'usage': {'calls': 1}}
    (recovery_directory / 'partial.json').write_text(json.dumps(recovery_record), 'utf-8')
    observation = recovered_observation({'request_id': 'partial', 'payload': {'units': [{'text': 'original'}],
        'manifest': recovery_manifest, 'audit_context': recovery_context}},
        SimpleNamespace(audit_dir=recovery_directory, ledger=second, manifest=recovery_manifest))
    check.assertEqual(observation, {'record': recovery_record, 'physical_status': 'ERROR',
                                     'physical_usage': {'calls': 1}})
    check.assertEqual(second.summary()['effective_usage_or_reserved'], {'calls': 2, 'tokens': 70})
    check.assertEqual(second.summary()['pending_or_unknown'], 1)
    for identity in ('unknown', 'partial'):
        with check.assertRaisesRegex(ValueError, 'already registered'):
            second.reserve(identity, {'calls': 1, 'tokens': 1})
    with check.assertRaisesRegex(ValueError, 'UNRESOLVED_PHYSICAL_REQUEST'):
        second.reserve('must-not-send', {'calls': 1, 'tokens': 1}, require_settled=True)
    with first.admission():
        with check.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_BUSY'):
            second.reserve('competitor', {'calls': 1, 'tokens': 1})
        first.reserve('committed-before-send', {'calls': 1, 'tokens': 10})
        check.assertEqual(second.summary()['physical_requests'], 3)
    first.finish('committed-before-send', status='COMPLETE', usage={'calls': 1, 'tokens': 2})
    for identity in ('Case', 'case', 'case '):
        first.reserve(identity, {'calls': 1, 'tokens': 1})
        first.finish(identity, status='COMPLETE', usage={'calls': 1, 'tokens': 1})
    check.assertEqual(second.summary()['physical_requests'], 6)
    check.assertEqual(second.registered(['case ', 'CASE', 'Case', 'case']), ['case ', 'Case', 'case'])
    check.assertEqual([second.entry(identity)['id'] for identity in ('Case', 'case', 'case ')],
                      ['Case', 'case', 'case '])
    source = Path(root) / 'legacy-import.sqlite3'
    source.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as db, db:
        db.execute('CREATE TABLE config(id INTEGER PRIMARY KEY,caps TEXT)')
        db.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,status TEXT,reserved TEXT,usage TEXT,category TEXT,diagnostic TEXT)')
        db.execute('INSERT INTO config VALUES(1,?)', (json.dumps(caps),))
        db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)',
                   ('retained-unknown', 'UNKNOWN', json.dumps({'calls': 1, 'tokens': 71}), None, 'synthetic', 'timeout'))
    original = source.read_bytes()
    frozen = inspect_ledger_import(source)
    imported = import_ledger(source, bind('physical-budget', 'imported'),
        expected_file_sha256=frozen['file_sha256'], expected_snapshot_sha256=frozen['snapshot_sha256'])
    check.assertEqual(imported.summary()['effective_usage_or_reserved'], {'calls': 1, 'tokens': 71})
    check.assertEqual(imported.summary()['pending_or_unknown'], 1)
    check.assertEqual(source.read_bytes(), original)
    old = BudgetLedger(source, caps)
    check.assertEqual(old.entry('retained-unknown'), imported.entry('retained-unknown'))
    check.assertEqual(old.registered(['missing', 'retained-unknown']), ['retained-unknown'])
    with check.assertRaisesRegex(ValueError, 'BUDGET_PRODUCER_SEALED'):
        old.reserve('old-writer', {'calls': 1, 'tokens': 1})
    restored = import_ledger(source, bind('physical-budget', 'imported'),
        expected_file_sha256=frozen['file_sha256'], expected_snapshot_sha256=frozen['snapshot_sha256'])
    check.assertEqual(restored.summary(), imported.summary())
    receipts = RequestReceipts(database=bind('request-receipts', 'receipts'))
    reopened_receipts = RequestReceipts(database=bind('request-receipts', 'receipts'))
    receipt_barrier = Barrier(2, timeout=15)

    def concurrent_begin(store):
        receipt_barrier.wait()
        try:
            return store.begin('logical', 'payload-hash', {})
        except RuntimePolicyError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(concurrent_begin, selected) for selected in (receipts, reopened_receipts)]
        outcomes = [attempt.result(timeout=35) for attempt in attempts]
    winners = [value for value in outcomes if isinstance(value, str)]
    check.assertEqual(len(winners), 1, outcomes)
    check.assertEqual([value.code for value in outcomes if isinstance(value, RuntimePolicyError)],
                      ['PROVIDER_REQUEST_UNRESOLVED'])
    physical = winners[0]
    with check.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
        reopened_receipts.begin('logical', 'payload-hash', {})
    receipts.finish('logical', physical, 'UNKNOWN')
    with check.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_UNRESOLVED'):
        reopened_receipts.begin('logical', 'payload-hash', {'retry_of': physical, 'retry_reason': 'retry'})
    reopened_receipts.reconcile('logical', physical, outcome='ERROR', evidence_reference='synthetic evidence')
    retry = receipts.begin('logical', 'payload-hash', {'retry_of': physical, 'retry_reason': 'explicit-retry'})
    check.assertNotEqual(retry, physical)
    receipts.finish('logical', retry, 'COMPLETE')
    with check.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_REQUEST_ALREADY_SETTLED'):
        reopened_receipts.begin('logical', 'payload-hash', {})
    with check.assertRaisesRegex(RuntimePolicyError, 'INVALID_REQUEST_RECONCILIATION'):
        reopened_receipts.reconcile('logical', physical, outcome='ERROR', evidence_reference='stale evidence')
    provider = SimpleNamespace(provider_id='synthetic', model='model', manifest={}, base_url='https://fixture.invalid')
    admission = ProviderAdmission(provider, database=bind('provider-admission', 'admission'), objects=objects)
    admission.block('a' * 32, payload_sha256='payload', category='synthetic')
    restored_admission = ProviderAdmission(provider, database=bind('provider-admission', 'admission'), objects=objects)
    with check.assertRaises(CaptureAdmissionError):
        restored_admission.require_available()
    blocked = restored_admission.snapshot()
    with check.assertRaisesRegex(ValueError, 'CHECKPOINT_CHANGED'):
        restored_admission.acknowledge(expected_checkpoint='stale', evidence_reference='synthetic repair')
    restored_admission.acknowledge(expected_checkpoint=blocked['checkpoint'], evidence_reference='synthetic repair')
    restored_admission.require_available()
    check.assertEqual(restored_admission.snapshot()['state'], 'AVAILABLE')
    model_ledger = BudgetLedger(caps={'requests': 1}, database=bind('physical-model', 'model'))
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'synthetic response'},
                                                     'finish_reason': 'stop'}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAICompatibleAdapter(provider='synthetic', model='model', endpoint='https://fixture.invalid',
            api_key='synthetic', capabilities=[Capability.CHAT], ledger=model_ledger,
            reservation={'requests': 1}, client=client, audit_root=Path(root) / 'model-calls',
            admission_factory=lambda selected: ProviderAdmission(selected,
                database=bind('model-admission', 'model-admission'), objects=objects))
        response = adapter.invoke(ModelRequest(Capability.CHAT, {'messages': [{'role': 'user', 'content': 'test'}]}))
        check.assertEqual(response.data, {'text': 'synthetic response', 'finish_reason': 'stop'})
    check.assertEqual(len(sent), 1)
    check.assertEqual(model_ledger.summary()['effective_usage_or_reserved'], {'requests': 1})
    return {'engine': first.database.info.engine, 'cache': 'exact-key-current-authorization-blob-integrity',
            'physical_requests': second.summary()['physical_requests'], 'unknown_reservation': 40,
            'admission': 'cross-client-exclusion-reservation-visible-before-send',
            'legacy_import': 'explicit-hash-checked-source-readonly-sealed-and-idempotent',
            'identity_comparison': 'case-and-trailing-space-exact', 'new_provider_calls': 0,
            'request_receipts': 'unknown-blocks-until-explicit-evidence-linked-retry-new-identity',
            'provider_admission': 'blob-backed-immutable-events-and-checkpointed-acknowledgement',
            'synthetic_model_attempts': len(sent)}


class PersistenceContracts(unittest.TestCase):
    def test_actual_sqlite_injected_storage_obeys_shared_rules(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service, blobs = DatabaseService(), BlobService()
            self.addCleanup(service.close)
            self.addCleanup(blobs.close)
            service.register('local', SQLiteProvider(root / 'database'))
            blobs.register('local', FileBlobProvider(root / 'objects'))

            def bind(owner, space):
                return service.bind(service.grant('local', SpaceKey('persistence', owner, space), 'fixture'))

            result = exercise_persistence(bind, blobs.bind('local', space='cache'), root / 'migration')
            self.assertEqual(result['physical_requests'], 6)

    def test_legacy_ledger_cannot_be_silently_replaced_or_mutated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE config(id INTEGER PRIMARY KEY,caps TEXT)')
                db.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,status TEXT,reserved TEXT,usage TEXT,category TEXT,diagnostic TEXT)')
                db.execute('INSERT INTO config VALUES(1,?)', ('{"calls":3}',))
            before = sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'EXPLICIT_IMPORT'):
                BudgetLedger(path, {'calls': 3})
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), before)

    def test_file_mapping_rejects_ungranted_space_and_duplicate_paths(self):
        with TemporaryDirectory() as directory:
            a, b = SpaceKey('a', 'owner', 'space'), SpaceKey('b', 'owner', 'space')
            path = Path(directory) / 'file.sqlite3'
            with self.assertRaisesRegex(ValueError, 'DUPLICATE_SQLITE_SPACE_FILE'):
                SQLiteProvider(directory, files={a: path, b: path})
            provider = SQLiteProvider(directory, files={a: path})
            self.addCleanup(provider.close)
            with self.assertRaisesRegex(ValueError, 'NOT_GRANTED'):
                provider.open_space(b)
