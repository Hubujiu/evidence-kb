import ast
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from kb_platform.agent_tools import EvidenceGateway
from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.database_providers import SQLiteProvider
from kb_platform.documents import parse_native_document
from kb_platform.embedded_search import EmbeddedSearch
from kb_platform.embedding_jobs import EmbeddingJobError, EmbeddingJobs
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import EMBEDDING_ATTEMPTS, EMBEDDING_JOBS
from kb_platform.knowledge_semantic_contract import METHODS, SemanticClient, SemanticEndpoint
from kb_platform.knowledge_storage import KnowledgeRepository, KnowledgeTransaction
from kb_platform.plugin_contracts import ServiceEndpoint
from kb_platform.semantic_pages_v2 import PROTOCOL, SemanticPageService
from kb_platform.storage import OriginalStore
from kb_platform.storage_adapters.sql import SqlStorageProvider
from kb_platform.storage_sdk import ExistingDataset, SpaceKey
from .contracts import digest
from .knowledge_persistence_contracts import LocalEmbeddingProvider
from .formal_state_machines import registry_violations, source_digest
from scripts.semantic_manifest_checks import semantic_manifest_violations


class PublicationEmbeddingContracts(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        storage = SqlStorageProvider(SQLiteProvider(self.root / 'database'))
        self.addCleanup(storage.close)
        space = SpaceKey('formal-contract', 'knowledge', 'publication')
        identity = storage.initialize('structured', space)
        database = next(export for export in storage.exports() if export.name == 'structured')\
            .open_space(space, ExistingDataset(identity.dataset_id))
        self.repository = KnowledgeRepository(database=database)
        self.jobs = EmbeddingJobs(self.repository)
        self.provider = LocalEmbeddingProvider()
        self.scope, self.units, self.audit = ('tenant', 'kb', 'build'), [{'text': 'synthetic'}], {'acl': 1}

    def _platform(self):
        search, store = EmbeddedSearch(self.root / 'search'), OriginalStore(self.root / 'originals')
        self.addCleanup(search.close)
        self.addCleanup(store.close)
        platform = Platform(repository=self.repository, search=search, store=store,
            parser=parse_native_document, embedding_jobs=self.jobs)
        owner, reader = Actor('tenant', 'owner'), Actor('tenant', 'reader')
        kb_id = platform.create_kb(owner, 'Synthetic publication')['kb_id']
        platform.set_member(owner, kb_id, reader.principal_id, ['kb.read'])
        source = platform.ingest(owner, kb_id, 'fixture.md', b'Synthetic publication source.', readers=['reader'])
        build = platform.stage(owner, kb_id)
        platform.publish(owner, kb_id, build['build_id'])
        return platform, owner, reader, kb_id, source

    def test_public_semantic_manifest_rechecks_authority_after_storage_read(self):
        for mode in ('durable', 'file'):
            for change in ('none', 'revision', 'manifest', 'membership'):
                with self.subTest(mode=mode, change=change):
                    platform, owner, reader, kb_id, source = self._platform()
                    gateway = EvidenceGateway(platform)
                    state = gateway.authorize(reader, kb_id)
                    provider = SimpleNamespace(provider_id='offline', dimension=2,
                        manifest={'provider': 'offline', 'dimension': 2, 'max_tokens': 100})
                    service = SemanticPageService(platform, provider, self.root / 'semantic', gateway=gateway)
                    manifest = dict(tenant_id=reader.tenant_id, kb_id=kb_id, protocol=PROTOCOL,
                        embedding=provider.manifest, dependencies={}, provider_id='offline', dimension=2,
                        build_id=state['manifest_id'], authority_revision=state['authority_revision'])
                    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'), allow_nan=False)
                    checksum = hashlib.sha256(serialized.encode()).hexdigest()
                    successor = platform.stage(owner, kb_id) if change == 'manifest' else None
                    changed = False

                    def mutate():
                        nonlocal changed
                        if changed:
                            return
                        changed = True
                        if change == 'revision':
                            platform.set_source_readers(owner, source['source_id'], [])
                        elif change == 'manifest':
                            platform.publish(owner, kb_id, successor['build_id'])
                        elif change == 'membership':
                            platform.set_member(owner, kb_id, reader.principal_id, [])

                    def read_manifest(*args):
                        mutate()
                        return deepcopy(manifest), checksum

                    def dependencies():
                        if mode == 'file':
                            mutate()
                        return {}

                    if mode == 'durable':
                        service.platform = SimpleNamespace(backend=SimpleNamespace(
                            supports_manifest_storage=True, current_manifest=read_manifest))
                    else:
                        directory = service._directory(reader, kb_id)
                        directory.mkdir(parents=True)
                        (directory / 'manifest.json').write_text(serialized, 'utf-8')
                        (directory / 'active.json').write_text(json.dumps(
                            {'manifest': 'manifest.json', 'sha256': checksum}), 'utf-8')
                    service._dependencies = dependencies
                    client = SemanticClient(ServiceEndpoint(SemanticEndpoint(service), METHODS))
                    if change == 'none':
                        self.assertEqual(client.current_manifest(reader, kb_id), (manifest, checksum))
                    else:
                        with self.assertRaises(PlatformError) as rejected:
                            client.current_manifest(reader, kb_id)
                        self.assertEqual(rejected.exception.code,
                            'ACCESS_DENIED' if change == 'membership' else 'SCOPE_CHANGED')

    def _claim(self):
        _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
        return self.jobs._claim(*self.scope, 0, dependency), dependency

    def _expire(self):
        with self.jobs._tx(*self.scope[:2]) as transaction:
            now = transaction.execute(K.JOB_CLOCK).fetchone()['db_now']
            transaction._transaction.execute(update(EMBEDDING_JOBS).where(
                EMBEDDING_JOBS.c.build_id == self.scope[2]).values(lease_until=now - timedelta(seconds=1)))

    def _attempts(self):
        with self.jobs._tx(*self.scope[:2]) as transaction:
            return transaction._transaction.execute(select(EMBEDDING_ATTEMPTS)).fetchall()

    def test_cancelled_worker_is_unknown_not_unsent_or_blindly_replayed(self):
        class Cancelled(BaseException):
            pass

        calls = []

        def cancelled(texts):
            calls.append(texts)
            raise Cancelled()

        self.provider.documents = cancelled
        with self.assertRaises(Cancelled):
            self.jobs.run(*self.scope, self.provider, self.units, lambda: True, self.audit)
        self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'RUNNING')
        self._expire()
        restarted = EmbeddingJobs(self.repository)
        self.assertEqual(restarted.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
        with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_UNKNOWN'):
            restarted.run(*self.scope, self.provider, self.units, lambda: True, self.audit)
        self.assertEqual(len(calls), 1)
        self.assertEqual([row['status'] for row in self._attempts()], ['UNKNOWN'])

    def test_late_completion_cannot_settle_successor_request(self):
        first, dependency = self._claim()
        unsent = EmbeddingJobError('synthetic-known-unsent')
        unsent.model_sent = False
        with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_RETRY'):
            self.jobs._finish(first, error=unsent)
        second = self.jobs._claim(*self.scope, 0, dependency)
        before = self.jobs.status(*self.scope), self._attempts()
        with self.assertRaisesRegex(EmbeddingJobError, 'EMBEDDING_LEASE_LOST'):
            self.jobs._finish(first, vectors=[[1., 0.]])
        self.assertEqual((self.jobs.status(*self.scope), self._attempts()), before)
        self.jobs._finish(second, vectors=[[1., 0.]])
        self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'COMPLETE')

    def test_recovery_requires_exact_observation_and_never_resends(self):
        row, _ = self._claim()
        self._expire()
        record = dict(status='COMPLETE', task='document', request_id=row['request_id'],
            physical_request_id=row['request_id'], texts=['synthetic'], provider_manifest=self.provider.manifest,
            audit_context=self.audit, usage={'calls': 1}, vectors=[[1., 0.]], vectors_sha256=digest([[1., 0.]]))
        observation = dict(record=record, physical_status='COMPLETE', physical_usage={'calls': 1})
        self.provider.recover_embedding = lambda request: observation
        record['physical_request_id'] = 'unrelated'
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'UNKNOWN')
        record['physical_request_id'] = row['request_id']
        self.assertEqual(self.jobs.reconcile(*self.scope, self.provider, lambda: True)[0]['status'], 'COMPLETE')
        restarted = EmbeddingJobs(self.repository)
        self.assertEqual(restarted.run(*self.scope, self.provider, self.units, lambda: True, self.audit),
            [{'text': 'synthetic', 'vector': [1., 0.]}])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual([item['status'] for item in self._attempts()], ['COMPLETE'])

    def test_job_and_attempt_persistence_roll_back_together(self):
        _, dependency = self.jobs._prepare(*self.scope, self.provider, self.units, self.audit)
        execute = KnowledgeTransaction.execute

        def fail(query):
            def injected(transaction, selected, params=None):
                if selected == query:
                    raise RuntimeError('synthetic statement failure')
                return execute(transaction, selected, params)
            return injected

        with patch.object(KnowledgeTransaction, 'execute', fail(K.ATTEMPT_INSERT)):
            with self.assertRaisesRegex(RuntimeError, 'synthetic statement failure'):
                self.jobs._claim(*self.scope, 0, dependency)
        self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'PENDING')
        self.assertEqual(self._attempts(), [])
        row = self.jobs._claim(*self.scope, 0, dependency)
        with patch.object(KnowledgeTransaction, 'execute', fail(K.ATTEMPT_FINISH)):
            with self.assertRaisesRegex(RuntimeError, 'synthetic statement failure'):
                self.jobs._finish(row, vectors=[[1., 0.]])
        self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'RUNNING')
        self.assertEqual([item['status'] for item in self._attempts()], ['RUNNING'])
        self.jobs._finish(row, vectors=[[1., 0.]])
        self.assertEqual(self.jobs.status(*self.scope)[0]['status'], 'COMPLETE')

    def test_manifest_postflight_removal_is_a_source_lint_mutant(self):
        import shutil

        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))
        machine = next(item for item in registry['machines'] if item['id'] == 'search_generation')
        binding = next(item for item in machine['source_bindings']
            if item['symbol'] == 'SemanticPageService.current_manifest')
        self.assertEqual(binding['sha256'], source_digest(root, binding))
        with TemporaryDirectory() as directory:
            mutant_root = Path(directory)
            for name in ('kb_platform', 'rust', 'formal', 'scripts'):
                shutil.copytree(root / name, mutant_root / name, ignore=shutil.ignore_patterns('__pycache__'))
            path = mutant_root / binding['path']
            source = path.read_text('utf-8')
            guard = '            self.gateway._unchanged(actor, kb_id, state)\n'
            self.assertEqual(source.count(guard), 1)
            path.write_text(source.replace(guard, ''), 'utf-8')
            self.assertIn('search_generation: source review drift at '
                'kb_platform/semantic_pages_v2.py:SemanticPageService.current_manifest',
                registry_violations(mutant_root, registry))
            for owner in registry['machines']:
                for anchor in owner['source_bindings']:
                    if (anchor['path'], anchor['symbol']) == (binding['path'], binding['symbol']):
                        anchor['sha256'] = source_digest(mutant_root, anchor)
            refreshed = registry_violations(mutant_root, registry)
            self.assertFalse(any('source review drift at ' + binding['path'] + ':' + binding['symbol'] in error
                for error in refreshed))
            self.assertIn('authority_publication: semantic manifest postflight must be unconditional '
                'and immediately precede its result exit', refreshed)
            namespace = {'__name__': 'kb_platform._manifest_mutant', '__package__': 'kb_platform',
                         '__file__': str(path)}
            exec(compile(path.read_text('utf-8'), str(path), 'exec'), namespace)
            mutant = namespace['SemanticPageService'].current_manifest
            result = unittest.TestResult()
            with patch.object(SemanticPageService, 'current_manifest', mutant):
                unittest.TestSuite([PublicationEmbeddingContracts(
                    'test_public_semantic_manifest_rechecks_authority_after_storage_read')]).run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(result.errors, [])
            self.assertEqual(len(result.failures), 6)


    def test_semantic_manifest_structural_gate_rejects_refreshed_hash_bypasses(self):
        import shutil

        root = Path(__file__).resolve().parents[2]
        relative = 'kb_platform/semantic_pages_v2.py'
        original = ast.parse((root / relative).read_text('utf-8'))
        self.assertEqual(semantic_manifest_violations(ast.unparse(original)), [])
        variants = ('removed', 'before_reads', 'conditional', 'wrong_snapshot', 'early_return',
                    'work_after_guard', 'swallowed', 'recaptured', 'mutated_snapshot', 'handler_return')
        with TemporaryDirectory() as directory:
            mutant_root = Path(directory)
            for name in ('kb_platform', 'rust', 'formal', 'scripts'):
                shutil.copytree(root / name, mutant_root / name, ignore=shutil.ignore_patterns('__pycache__'))
            for variant in variants:
                tree = deepcopy(original)
                owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                             and node.name == 'SemanticPageService')
                method = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                              and node.name == 'current_manifest')
                protected = method.body[1]
                guard, result = protected.body[-2:]
                if variant == 'removed':
                    protected.body.remove(guard)
                elif variant == 'before_reads':
                    protected.body.remove(guard)
                    protected.body.insert(0, guard)
                elif variant == 'conditional':
                    protected.body[-2] = ast.If(test=ast.Constant(False), body=[guard], orelse=[])
                elif variant == 'wrong_snapshot':
                    guard.value.args[-1] = ast.Name(id='other_state', ctx=ast.Load())
                elif variant == 'early_return':
                    protected.body[0].body.append(deepcopy(result))
                elif variant == 'work_after_guard':
                    protected.body.insert(-1, ast.Expr(value=ast.Call(
                        func=ast.Name(id='read_more', ctx=ast.Load()), args=[], keywords=[])))
                elif variant == 'swallowed':
                    protected.body[-2] = ast.Try(body=[guard], handlers=[ast.ExceptHandler(
                        type=ast.Name(id='Exception', ctx=ast.Load()), name=None, body=[ast.Pass()])],
                        orelse=[], finalbody=[])
                elif variant == 'recaptured':
                    protected.body.insert(-2, deepcopy(method.body[0]))
                elif variant == 'mutated_snapshot':
                    protected.body.insert(-2, ast.parse(
                        'state.update(self.gateway.authorize(actor, kb_id))').body[0])
                else:
                    protected.handlers[0].body = [deepcopy(result)]
                source = ast.unparse(ast.fix_missing_locations(tree))
                with self.subTest(variant=variant):
                    self.assertTrue(semantic_manifest_violations(source))
                    (mutant_root / relative).write_text(source, 'utf-8')
                    registry = json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))
                    for machine in registry['machines']:
                        for binding in machine['source_bindings']:
                            if binding['path'] == relative:
                                binding['sha256'] = source_digest(mutant_root, binding)
                    errors = registry_violations(mutant_root, registry)
                    self.assertFalse(any('source review drift at ' + relative in error for error in errors))
                    self.assertTrue(any(error.startswith('authority_publication: semantic manifest') for error in errors))


if __name__ == '__main__':
    unittest.main()
