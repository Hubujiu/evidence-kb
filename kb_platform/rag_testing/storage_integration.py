from .test_outcomes import passed_contract_run

import argparse
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4
import xml.etree.ElementTree as ET

from sqlalchemy import create_engine, func, literal, select, text
from sqlalchemy.engine import make_url

from kb_platform.database_contracts import SpaceKey
from kb_platform.database_providers import MySQLProvider, PostgresProvider, SQLiteProvider
from kb_platform.database_service import DatabaseService
from kb_platform.storage_adapters.structured import StructuredSqlSpace
from kb_platform.storage_sdk import ExistingDataset, StorageExport
from kb_platform.storage_sdk.relational import GUARDED_RELATIONAL_V1
from kb_platform.storage_service import StorageService
from kb_platform.session_identity import Actor, PlatformError
from kb_platform.session_store import SessionStore
from kb_platform.session_transfer import export_sessions, import_sessions
from .runner import write_json
from .session_database_contracts import exercise_session_database


VARIABLES = ('KB_STORAGE_TEST_POSTGRES_URL', 'KB_STORAGE_TEST_DENSE_POSTGRES_URL',
             'KB_STORAGE_TEST_MYSQL_URL', 'KB_STORAGE_TEST_S3_ENDPOINT',
             'KB_STORAGE_TEST_S3_ACCESS_KEY', 'KB_STORAGE_TEST_S3_SECRET_KEY', 'KB_STORAGE_TEST_REDIS_URL')
ROOT = Path(__file__).resolve().parents[2]
TEXT = 'QUARTZMARKER Publication requires owner review before release.'


def acceptance_url(value, backend):
    url = make_url(value)
    if (url.get_backend_name() != backend or url.host != '127.0.0.1' or not url.port
            or not (url.database or '').startswith('rust_plugin_acceptance_')
            or url.query):
        raise ValueError('Storage integration requires a dedicated loopback acceptance database')
    return url


def configuration(environment):
    missing = [name for name in VARIABLES if not environment.get(name)]
    if missing:
        raise ValueError('Storage integration requires ' + ', '.join(missing))
    postgres = acceptance_url(environment[VARIABLES[0]], 'postgresql')
    dense = acceptance_url(environment[VARIABLES[1]], 'postgresql')
    mysql = acceptance_url(environment[VARIABLES[2]], 'mysql')
    if (postgres.host, postgres.port, postgres.database) == (dense.host, dense.port, dense.database):
        raise ValueError('BM25 and dense acceptance must use distinct actual databases')
    from urllib.parse import urlsplit

    endpoint = urlsplit(environment['KB_STORAGE_TEST_S3_ENDPOINT'])
    if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or not endpoint.port
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path not in ('', '/')):
        raise ValueError('S3 acceptance requires a loopback HTTP endpoint')
    cache = urlsplit(environment['KB_STORAGE_TEST_REDIS_URL'])
    if (cache.scheme != 'redis' or cache.hostname != '127.0.0.1' or not cache.port
            or cache.username or cache.password or cache.query or cache.fragment or cache.path != '/0'):
        raise ValueError('Redis acceptance requires a dedicated loopback cache')
    return {'postgresql': postgres, 'dense_postgresql': dense, 'mysql': mysql,
            's3_endpoint': environment['KB_STORAGE_TEST_S3_ENDPOINT']}


class StorageFixtures:
    def __init__(self, selected, output, observations, environment=None):
        self.selected, self.output, self.observations = selected, output, observations
        self.environment = dict(os.environ if environment is None else environment)
        self.run_id, self.services, self.spaces, self.buckets = uuid4().hex, [], [], []
        (ROOT / '.local').mkdir(exist_ok=True)
        self.temporary = TemporaryDirectory(prefix='storage-acceptance-resources-', dir=ROOT / '.local')
        self.resource_root = Path(self.temporary.name).resolve()

    def bind(self, engine, name, *, consumer='fixture', owner='sessions'):
        service = DatabaseService()
        self.services.append(service)
        if engine == 'sqlite':
            provider = SQLiteProvider(self.resource_root / 'sqlite')
        else:
            provider_type = PostgresProvider if engine == 'postgresql' else MySQLProvider
            provider = provider_type(self.selected[engine])
        service.register('acceptance', provider)
        key = SpaceKey('storage-acceptance-' + self.run_id, owner, name)
        if engine != 'sqlite':
            self.spaces.append((engine, 'kb_' + key.digest[:24]))
        bound = service.bind(service.grant('acceptance', key, consumer_identity=consumer))
        self.observations.setdefault('bindings', []).append({
            'engine': engine, 'identity': asdict(bound.identity), 'physical_name': bound.physical_name,
            'runtime_role': bound.runtime_role, 'registration_id': bound.registration_id})
        return service, bound

    def pair(self, engine, name):
        service, bound = self.bind(engine, name, consumer='first')
        key = bound.identity.space
        other = service.bind(service.grant('acceptance', key, consumer_identity='second'))
        if bound._grant.token == other._grant.token:
            raise ValueError('Session clients must receive independent grants')
        self.observations.setdefault('session_grants', []).append({
            'engine': engine, 'first': bound._grant.token, 'second': other._grant.token})
        return SessionStore(database=self._structured_bound(bound)), SessionStore(database=self._structured_bound(other))

    def _structured_bound(self, bound):
        registry = StorageService()
        self.services.append(registry)
        registry.register('structured', StorageExport('structured', (GUARDED_RELATIONAL_V1,),
            lambda space, target: StructuredSqlSpace(bound._space)), owner_lease=bound._lease)
        grant = registry.grant('structured', GUARDED_RELATIONAL_V1, bound.identity.space, 'fixture',
                               target=ExistingDataset(bound.identity.dataset_id))
        return registry.bind(grant)

    def structured(self, engine, name, *, consumer='fixture', owner='sessions'):
        service, bound = self.bind(engine, name, consumer=consumer, owner=owner)
        return service, self._structured_bound(bound)

    def structured_admin(self, engine, name, *, owner):
        service, bound = self.bind(engine, name, owner=owner)
        return service, StructuredSqlSpace(bound._space)

    def close(self):
        errors = []
        for service in reversed(self.services):
            try:
                service.close()
            except Exception as error:
                errors.append(type(error).__name__ + ': ' + str(error))
        for engine, name in reversed(tuple(dict.fromkeys(self.spaces))):
            if not re.fullmatch(r'kb_[0-9a-f]{24}', name):
                raise ValueError('Unexpected acceptance namespace')
            url = self.selected[engine]
            postgres = engine in ('postgresql', 'dense_postgresql')
            driver = 'postgresql+psycopg' if postgres else 'mysql+pymysql'
            admin = create_engine(url.set(drivername=driver))
            try:
                with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                    quote = connection.dialect.identifier_preparer.quote
                    statement = ('DROP SCHEMA IF EXISTS ' + quote(name) + ' CASCADE' if postgres
                                 else 'DROP DATABASE IF EXISTS ' + quote(name))
                    connection.exec_driver_sql(statement)
            except Exception as error:
                errors.append(type(error).__name__ + ': ' + str(error))
            finally:
                admin.dispose()
        for client, bucket in self.buckets:
            try:
                for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
                    objects = [{'Key': row['Key']} for row in page.get('Contents', [])]
                    if objects:
                        client.delete_objects(Bucket=bucket, Delete={'Objects': objects})
                client.delete_bucket(Bucket=bucket)
            except Exception as error:
                errors.append(type(error).__name__ + ': ' + str(error))
            finally:
                client.close()
        if (self.resource_root.parent != (ROOT / '.local').resolve()
                or not self.resource_root.name.startswith('storage-acceptance-resources-')):
            raise ValueError('Unexpected acceptance resource cleanup path')
        self.temporary.cleanup()
        self.observations['cleanup'] = {'namespaces': len(set(self.spaces)), 'buckets': len(self.buckets),
                                        'local_resources_removed': not self.resource_root.exists(), 'errors': errors}
        if errors:
            raise RuntimeError('Storage acceptance cleanup failed: ' + '; '.join(errors))


def session_suite(fixtures):
    class SessionIntegration(unittest.TestCase):
        def test_three_actual_engines_share_session_behavior(self):
            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    first, second = fixtures.pair(engine, 'contract-' + engine)
                    result = exercise_session_database(first, second, engine=engine)
                    self.assertEqual(result['engine'], engine)
                    self.assertEqual(result['revision'], 5)
                    self.assertIsNot(first.database, second.database)
                    with first.database.transaction(write=False) as tx:
                        identity = tx.execute(select(literal(1).label('actual_connection'))).fetchone()
                    self.assertEqual(identity['actual_connection'], 1)
                    fixtures.observations.setdefault('sessions', []).append(result)

        def test_six_cross_engine_clones_preserve_records_and_unknown(self):
            actor = Actor('synthetic-tenant', 'owner')
            engines = ('sqlite', 'postgresql', 'mysql')
            for source_engine in engines:
                _, bound = fixtures.structured(source_engine, 'clone-source-' + source_engine)
                source = SessionStore(database=bound)
                sid = source.create(actor, 'scope', {'source': source_engine})['session_id']
                token = source.begin(actor, sid, 'complete', 0, 'fingerprint')['token']
                artifact = source.artifact(actor, sid, token, {'quote': '不可变原文'})
                expected = source.finish(actor, sid, token, {'source': source_engine, 'done': True},
                                         {'status': 'complete', 'answer': '克隆后仍可重放'})
                source.begin(actor, sid, 'unknown', 1, 'unknown-fingerprint')
                package = export_sessions(source, source_frozen=True)
                for target_engine in engines:
                    if target_engine == source_engine:
                        continue
                    with self.subTest(source=source_engine, target=target_engine):
                        _, target_bound = fixtures.structured(target_engine, source_engine + '-to-' + target_engine)
                        target = SessionStore(database=target_bound)
                        cloned = import_sessions(target, package, target_offline=True)
                        self.assertNotEqual(cloned['source_dataset_id'], cloned['target_dataset_id'])
                        self.assertEqual(target.begin(actor, sid, 'complete', 0, 'fingerprint'), {'cached': expected})
                        self.assertEqual(target.read_artifact(actor, sid, artifact['artifact_id']), {'quote': '不可变原文'})
                        self.assertEqual(export_sessions(target, source_frozen=True)['tables'], package['tables'])
                        with self.assertRaises(PlatformError) as unknown:
                            target.begin(actor, sid, 'unknown', 1, 'unknown-fingerprint')
                        self.assertEqual(unknown.exception.code, 'TURN_IN_PROGRESS')
                        fixtures.observations.setdefault('clones', []).append({
                            'source': source_engine, 'target': target_engine,
                            'records': sum(len(rows) for rows in package['tables'].values()),
                            'unknown_preserved': True, 'sha256': package['sha256']})

        def test_namespaces_isolate_same_table_and_runtime_cannot_create(self):
            actor = Actor('tenant', 'owner')
            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    _, a = fixtures.structured(engine, 'isolation-first-' + engine)
                    _, b = fixtures.structured(engine, 'isolation-second-' + engine)
                    first, second = SessionStore(database=a), SessionStore(database=b)
                    sid = first.create(actor, 'scope', {})['session_id']
                    self.assertEqual(second.list_sessions(actor), [])
                    with self.assertRaises(PlatformError) as denial:
                        second.load(actor, sid)
                    self.assertEqual(denial.exception.code, 'SESSION_NOT_FOUND')
                    self.assertNotEqual(a.identity.dataset_id, b.identity.dataset_id)
                    with self.assertRaises(Exception):
                        with a.transaction() as tx:
                            tx.execute('CREATE TABLE forbidden_runtime_ddl (id INTEGER)')
                    with a.transaction(write=False) as tx:
                        from kb_platform.session_storage.schema import metadata

                        count = tx.execute(select(func.count().label('count')).select_from(metadata.tables['sessions'])).scalar()
                    self.assertEqual(count, 1)

        def test_real_cache_budget_unknown_and_explicit_ledger_import(self):
            from kb_platform.blob_service import BlobService
            from kb_platform.storage import FileBlobProvider
            from .persistence_contracts import exercise_persistence

            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    objects = BlobService()
                    objects.register('fixture', FileBlobProvider(fixtures.resource_root / ('cache-objects-' + engine)))
                    try:
                        def bind(owner, space):
                            return fixtures.structured(engine, 'persistence-' + space, owner=owner)[1]

                        result = exercise_persistence(bind, objects.bind('fixture', space=engine),
                            fixtures.resource_root / ('ledger-migration-' + engine), engine=engine)
                        self.assertEqual(result['engine'], engine)
                        fixtures.observations.setdefault('persistence', []).append(result)
                    finally:
                        objects.close()

    return unittest.defaultTestLoader.loadTestsFromTestCase(SessionIntegration)


def foundation_suite(fixtures):
    class FoundationStorageIntegration(unittest.TestCase):
        def test_actual_engine_migration_journals_preserve_failure_boundaries(self):
            from .persistent_storage_contracts import exercise_migration_journal

            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    _, bound = fixtures.bind(engine, 'migration-journal', owner='journal')
                    result = exercise_migration_journal(bound)
                    fixtures.observations.setdefault('migration_journals', []).append(result)

        def test_actual_mysql_release_error_discards_session_lock(self):
            from .persistent_storage_contracts import exercise_mysql_release_failure

            _, bound = fixtures.bind('mysql', 'migration-release-failure', owner='journal')
            fixtures.observations['migration_release'] = exercise_mysql_release_failure(bound)

        def test_actual_engines_reject_transaction_escape_without_committing_writes(self):
            from .database_foundation_contracts import exercise_transaction_atomicity, task_schema

            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    _, bound = fixtures.bind(engine, 'transaction-atomicity', owner='tasks')
                    bound.prepare(task_schema(bound.identity.space.owner_id))
                    bound.seal()
                    observations = exercise_transaction_atomicity(bound)
                    self.assertEqual(len(observations), 26)
                    self.assertEqual({row['persisted_writes'] for row in observations}, {0})
                    fixtures.observations.setdefault('transaction_atomicity', []).extend(observations)

        def test_actual_mysql_rejects_nontransactional_schema_before_runtime_access(self):
            from .database_foundation_contracts import exercise_mysql_transactional_schema

            _, bound = fixtures.bind('mysql', 'nontransactional-schema', owner='tasks')
            result = exercise_mysql_transactional_schema(bound)
            self.assertEqual(result['nontransactional_schema'], 'REJECTED')
            self.assertEqual(result['runtime_write'], 'DENIED')
            fixtures.observations['nontransactional_schema'] = result

        def test_actual_mysql_removes_migration_grants_to_reserved_metadata(self):
            from sqlalchemy.exc import OperationalError

            from kb_platform.database_contracts import DatabaseError, Migration, SchemaPackage

            _, bound = fixtures.bind('mysql', 'reserved-metadata-grants', owner='tasks')
            package = SchemaPackage('tasks', (Migration(1, (
                'CREATE TABLE tasks(id VARCHAR(64) PRIMARY KEY,value TEXT NOT NULL)',
                'GRANT SELECT,INSERT,UPDATE,DELETE ON __DATABASE_SCHEMA__.* TO __DATABASE_RUNTIME_ROLE__',
            )),))
            bound.prepare(package)
            bound.seal()

            def snapshot():
                with bound._space.migration_engine.connect() as audit:
                    identity = audit.exec_driver_sql('SELECT * FROM _database_identity').mappings().all()
                    migrations = audit.exec_driver_sql('SELECT * FROM _database_migrations').mappings().all()
                    return {'identity': [dict(row) for row in identity], 'migrations': [dict(row) for row in migrations]}

            original = snapshot()
            with bound.transaction() as tx:
                tx.execute("INSERT INTO tasks VALUES('allowed','business write')")
            rejected = []
            for table, column in (('_database_identity', 'dataset_id'), ('_database_migrations', 'content_hash')):
                with self.assertRaises(OperationalError) as denial:
                    with bound.transaction() as tx:
                        tx.execute('UPDATE ' + table + ' SET ' + column + '=' + column)
                self.assertEqual(denial.exception.orig.args[0], 1142)
                rejected.append(table)
                limited = MySQLProvider(bound._space.runtime_engine.url,
                    migration_url=bound._space.migration_engine.url, provision=False, database=bound.physical_name)
                with bound._space.migration_engine.begin() as audit:
                    quote = audit.dialect.identifier_preparer.quote
                    target = quote(bound.physical_name) + '.' + quote(table)
                    account = "'" + bound.runtime_role + "'@'%'"
                    audit.execute(text('GRANT UPDATE ON ' + target + ' TO ' + account))
                try:
                    with self.assertRaisesRegex(DatabaseError, 'DATABASE_RUNTIME_METADATA_WRITE_PRIVILEGE'):
                        limited.open_space(bound.identity.space)
                finally:
                    limited.close()
                    with bound._space.migration_engine.begin() as audit:
                        audit.execute(text('REVOKE UPDATE ON ' + target + ' FROM ' + account))
            self.assertEqual(snapshot(), original)
            fixtures.observations['mysql_metadata_permissions'] = {
                'migration_database_wide_grant': 'removed', 'runtime_metadata_writes_denied': rejected,
                'unsafe_preconfigured_accounts': 'rejected', 'original_metadata_unchanged': True,
                'decision': 'PASS'}

    return unittest.defaultTestLoader.loadTestsFromTestCase(FoundationStorageIntegration)


@contextmanager
def running_host(directory, environment, profile, name):
    import httpx

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    stop = directory / (name + '.stop')
    host_profile = directory / (name + '.json')
    write_json(host_profile, {'version': 1, 'worker': [sys.executable, '-B', '-u', '-m', 'kb_platform.host_worker'],
        'cwd': str(ROOT), 'stop_file': str(stop), 'startup_timeout_ms': 30000, 'shutdown_timeout_ms': 10000,
        'application': {'factory': 'kb_platform.rag_testing.storage_e2e_app:api',
                        'config': {'application_profile': str(profile), 'directory': str(directory),
                                   'host': '127.0.0.1', 'port': port, 'log_level': 'warning'}}})
    with (directory / (name + '.stdout.log')).open('w', encoding='utf-8') as stdout, (
            directory / (name + '.stderr.log')).open('w', encoding='utf-8') as stderr:
        process = subprocess.Popen([environment['KB_PLUGIN_HOST_BINARY'], '--profile', str(host_profile)],
            cwd=ROOT, env=environment, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 40
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=45) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('Rust storage host exited; see ' + str(directory / (name + '.stderr.log')))
                    try:
                        if client.get('/health', timeout=1).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Storage HTTP readiness deadline exceeded')
                    time.sleep(0.1)
                yield client, f'http://127.0.0.1:{port}'
        finally:
            stop.touch()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError('Storage Rust host did not stop gracefully') from None
        receipt = json.loads((directory / (name + '.stdout.log')).read_text('utf-8'))
        if process.returncode or receipt.get('status') != 'completed':
            raise RuntimeError('Storage Rust host lifecycle failed')


def http_configuration(fixtures, engine):
    if engine not in {'postgres', 'embedded', 'sqlite', 'mysql'}:
        raise ValueError('Unknown HTTP storage acceptance arrangement')
    directory = fixtures.output / ('http-' + engine)
    directory.mkdir()
    application = 'storage-http-' + fixtures.run_id + '-' + engine
    tokens = {'owner': 'owner-' + uuid4().hex, 'denied': 'denied-' + uuid4().hex,
              'other': 'other-' + uuid4().hex}
    write_json(directory / 'identities.json', {tokens[name]: {
        'tenant_id': fixtures.run_id + ('-other' if name == 'other' else ''),
        'principal_id': name} for name in tokens})
    from kb_platform.storage_catalog import initialize_configuration

    instances = {
        'main': {'provider': 'postgresql', 'config': {'url_env': 'KB_STORAGE_TEST_POSTGRES_URL'}},
        'documents': {'provider': 'sqlite', 'config': {'root': str(fixtures.resource_root / ('documents-' + engine))}},
        'dense': ({'provider': 'postgresql', 'config': {'url_env': 'KB_STORAGE_TEST_DENSE_POSTGRES_URL'}}
                  if engine == 'postgres' else
                  {'provider': 'embedded', 'config': {'root': str(fixtures.resource_root / 'dense-indexes')}}),
    }
    if engine == 'sqlite':
        instances['authority'] = {'provider': 'sqlite', 'config': {
            'root': str(fixtures.resource_root / 'sqlite-authority')}}
    elif engine == 'mysql':
        instances['authority'] = {'provider': 'mysql', 'config': {'url_env': 'KB_STORAGE_TEST_MYSQL_URL'}}
    services = {
        'main.sql': {'instance': 'authority' if engine in ('sqlite', 'mysql') else 'main', 'export': 'structured'},
        'main.lexical': {'instance': 'main', 'export': 'lexical'},
        'dense.vector': {'instance': 'dense', 'export': 'vector'},
        'documents.records': {'instance': 'documents', 'export': 'document'},
        'originals.objects': {'instance': 'originals', 'export': 'object'},
    }
    bindings = {
        'repository': {'primary': {'service': 'main.sql', 'owner': 'knowledge', 'space': 'content'}},
        'search': {
            'bm25': {'service': 'main.lexical', 'owner': 'knowledge', 'space': 'lexical'},
            'dense': {'service': 'dense.vector', 'owner': 'knowledge', 'space': 'vector'},
            'source': {'service': 'documents.records', 'owner': 'knowledge', 'space': 'evidence'},
        },
        'originals': {'objects': {'service': 'originals.objects', 'owner': 'knowledge', 'space': 'originals'}},
    }
    if engine == 'postgres':
        import boto3
        from botocore.config import Config

        bucket = 'rust-plugin-acceptance-' + fixtures.run_id
        client = boto3.client('s3', endpoint_url=fixtures.selected['s3_endpoint'],
            aws_access_key_id=fixtures.environment['KB_STORAGE_TEST_S3_ACCESS_KEY'],
            aws_secret_access_key=fixtures.environment['KB_STORAGE_TEST_S3_SECRET_KEY'],
            config=Config(proxies={}, s3={'addressing_style': 'path'}, retries={'max_attempts': 0}))
        client.create_bucket(Bucket=bucket)
        fixtures.buckets.append((client, bucket))
        instances['originals'] = {'provider': 's3', 'config': {
            'endpoint_url': fixtures.selected['s3_endpoint'], 'bucket': bucket,
            'access_key_env': 'KB_STORAGE_TEST_S3_ACCESS_KEY',
            'secret_key_env': 'KB_STORAGE_TEST_S3_SECRET_KEY'}}
    else:
        instances['originals'] = {'provider': 'files', 'config': {
            'root': str(fixtures.resource_root / ('originals-' + engine))}}
    configuration = {'version': 2, 'application_id': application, 'instances': instances,
                     'services': services, 'bindings': bindings}
    for slots in bindings.values():
        for value in slots.values():
            instance = services[value['service']]['instance']
            provider = instances[instance]['provider']
            if provider not in ('postgresql', 'mysql'):
                continue
            key = SpaceKey(application, value['owner'], value['space'])
            database = ('mysql' if provider == 'mysql' else
                        'dense_postgresql' if instance == 'dense' else 'postgresql')
            fixtures.spaces.append((database, 'kb_' + key.digest[:24]))
    write_json(directory / 'storage.json', initialize_configuration(configuration))
    profile = directory / 'application.json'
    plugins = [{'id': 'capabilities', 'use': 'storage.fixture.capabilities'},
               {'id': 'search', 'use': 'platform.search.storage'},
               {'id': 'originals', 'use': 'platform.originals.storage'}]
    write_json(profile, {'version': 1, 'bundles': ['platform'], 'plugins': plugins})
    environment = {key: value for key, value in fixtures.environment.items()
                   if not key.startswith(('KB_', 'OPENAI_', 'GOOGLE_', 'ANTHROPIC_', 'AZURE_'))}
    environment.update({key: value for key, value in fixtures.environment.items()
                        if key.startswith('KB_STORAGE_TEST_') or key in ('KB_AGENT_KERNEL_LIBRARY', 'KB_PLUGIN_HOST_BINARY')})
    environment.update({'KB_STORAGE_CONFIG': str(directory / 'storage.json'),
        'KB_STORAGE_ROOT': str(fixtures.resource_root / ('originals-' + engine)), 'KB_IDENTITIES_FILE': str(directory / 'identities.json'),
        'KB_DEFER_DEFAULT_APP': '1', 'KB_REMOTE_MODELS': '0', 'KB_ENABLE_EMBEDDINGS': '0',
        'KB_AGENT_ROOT': str(fixtures.resource_root / ('sessions-' + engine)), 'PYTHONUTF8': '1',
        'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'})
    return directory, profile, environment, tokens


@contextmanager
def redis_http_authentication(fixtures, directory, profile, tokens):
    from kb_platform.authentication import SessionAuthentication
    from kb_platform.storage_adapters.redis_cache import RedisCacheProvider
    from kb_platform.storage_sdk import EphemeralCacheSpace

    configuration = json.loads((directory / 'storage.json').read_text('utf-8'))
    space = SpaceKey(configuration['application_id'], 'authentication', 'sessions')
    target = EphemeralCacheSpace('http-authentication-' + fixtures.run_id)
    configuration['instances']['auth'] = {'provider': 'redis', 'config': {
        'url_env': 'KB_STORAGE_TEST_REDIS_URL'}}
    configuration['services']['auth.sessions'] = {'instance': 'auth', 'export': 'kv'}
    configuration['bindings']['authentication'] = {'sessions': {'service': 'auth.sessions',
        'owner': space.owner_id, 'space': space.space_id, 'cache_id': target.cache_id}}
    write_json(directory / 'storage.json', configuration)
    selected = json.loads(profile.read_text('utf-8'))
    selected['plugins'].append({'id': 'authentication', 'use': 'authentication.sessions'})
    write_json(profile, selected)
    (directory / 'identities.json').unlink()
    provider = RedisCacheProvider(fixtures.environment['KB_STORAGE_TEST_REDIS_URL'])
    issued = []
    try:
        store = provider.open_space(space, target)
        auth = SessionAuthentication(store)
        for name in tokens:
            actor = Actor(fixtures.run_id + ('-other' if name == 'other' else ''), name)
            session = auth.issue(actor, ttl_seconds=600)
            issued.append(session.token)
            tokens[name] = session.token
        yield auth, store, issued
    finally:
        try:
            for token in issued:
                auth.revoke(token)
        finally:
            provider.close()


def score_http_evidence(client, headers, kb_id, source_id, version, directory):
    from kb_platform.agent_context import ContextManager, ContextPolicy
    from .storage_e2e_app import RecordingModelSink
    from .contracts import AdapterInput, Case, Evidence, Fragment, Plan, RequestEvidenceBinding, Source, Suite, Trace, digest
    from .runner import run as score

    source = Source(id=source_id, kb=kb_id, version=version, text=TEXT, sha256=sha256(TEXT.encode()).hexdigest())
    cases = tuple(Case(input=AdapterInput(case_id=channel, question='QUARTZMARKER publication review',
        kb=kb_id, principal='owner', acl_revision='actual-http-fixture-v1', allowed_sources=(source_id,),
        as_of='2026-11-01', context_budget=16000), family='storage-' + channel,
        any_of=(('rule',),), qrels={source_id: 1}) for channel in ('bm25', 'dense', 'hybrid'))
    suite = Suite(id='storage-http-synthetic-v1', label_provenance='synthetic_engineering', sources=(source,),
        evidence=(Evidence(id='rule', source_id=source_id, start=0, end=len(TEXT), quote=TEXT,
                           fact='Publication requires owner review before release.'),), cases=cases)

    class Adapter:
        implementation = 'actual-split-storage-http-context-manager/deterministic-recording-sink/v1'

        def run(self, request, sources, corpus):
            response = client.post(f'/knowledge-bases/{kb_id}/search', headers=headers,
                json={'query': request.question, 'channel': request.case_id, 'purpose': 'llm_context'})
            response.raise_for_status()
            hits = response.json()['hits']
            fragments, records = [], []
            for hit in hits:
                citation = client.get(f'/knowledge-bases/{kb_id}/citations/{hit["citation_id"]}', headers=headers)
                citation.raise_for_status()
                record = citation.json()
                if record['source_version_id'] != version or not record['anchors']:
                    raise ValueError('Actual source version or anchors changed')
                start = TEXT.index(hit['text'])
                fragments.append(Fragment(source_id=source_id, source_sha256=source.sha256, version=version,
                    kb=kb_id, start=start, end=start + len(hit['text']), text=hit['text']))
                records.append({**hit, 'source_id': source_id})
            prepared = ContextManager(ContextPolicy(input_budget=8000)).prepare(
                'answer', 'Use source evidence.', {}, [], records, request.question)
            sent = deepcopy(prepared.messages)
            sink = RecordingModelSink()
            sink.complete(prepared.messages)
            if sink.received != [sent]:
                raise ValueError('Recording sink received a different final request')
            windows = prepared.projection['evidence']
            final, bindings = [], []
            for index, window in enumerate(windows):
                if not window.get('text'):
                    continue
                start = TEXT.index(records[index]['text']) + window['text_start']
                final.append(Fragment(source_id=source_id, source_sha256=source.sha256, version=version,
                    kb=kb_id, start=start, end=start + len(window['text']), text=window['text']))
                bindings.append(RequestEvidenceBinding(message_index=len(sent) - 1,
                    json_path=('evidence', index, 'text'), fragment_index=len(final) - 1))
            write_json(directory / ('request-' + request.case_id + '.json'), sent)
            return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
                principal=request.principal, acl_revision=request.acl_revision, capture='pre_send',
                stages={'retrieval': tuple(fragments)}, final=tuple(final),
                final_context=json.dumps([{'source_id': item.source_id, 'text': item.text} for item in final]),
                request_messages=tuple(sent), request_evidence_bindings=tuple(bindings),
                model_calls=0, input_tokens=0, output_tokens=0)

    plan = Plan(suite_hash=digest(suite), mode='live', required_stages=('retrieval',), objective='evidence')
    return score(suite, plan, directory / 'evidence-scoring', adapter=Adapter())


def run_browser(directory, environment, url, token):
    node = shutil.which('node')
    cli = ROOT / 'e2e/node_modules/e2e/dist/cli/bin.js'
    if node is None or not cli.is_file():
        raise ValueError('Storage browser E2E requires installed locked e2e dependencies and Node')
    temporary = ROOT / 'e2e/.artifacts' / ('storage-' + uuid4().hex)
    selected = {**environment, 'KB_E2E_TESTS': 'storage.e2e.js', 'KB_E2E_URL': url,
                'KB_E2E_TOKEN': token, 'E2E_TELEMETRY_DISABLED': '1'}
    try:
        with (directory / 'browser.log').open('w', encoding='utf-8') as stream:
            result = subprocess.run([node, str(cli), 'run', '--output', str(temporary),
                '--no-cache', '--retries', '0'], cwd=ROOT / 'e2e', env=selected,
                stdout=stream, stderr=subprocess.STDOUT, timeout=180, check=False)
        if result.returncode:
            raise RuntimeError('Storage browser E2E failed; see browser.log')
        cases = list(ET.parse(temporary / 'junit.xml').getroot().iter('testcase'))
        if (len(cases) != 1 or cases[0].get('classname') != 'storage.e2e.js'
                or cases[0].get('name') != 'split storage browser upload, hybrid publication and dense citation [chromium]'
                or any(cases[0].find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                or (cases[0].findtext('system-out') or '').startswith('flaky:')):
            raise RuntimeError('Storage browser case must pass with no skips or retries')
        usage = json.loads((temporary / 'report.json').read_text('utf-8'))['run']
        if (usage['status'] != 'passed' or usage['errors'] or usage['usage']['modelTokens'] != 0
                or usage['usage']['maxModelCallsInStep'] != 0):
            raise RuntimeError('Storage browser requires deterministic steps with zero model calls')
        return {'tests': 1, 'status': 'PASS', 'usage': usage['usage']}
    finally:
        if temporary.exists():
            shutil.copytree(temporary, directory / 'browser')
            if temporary.resolve().parent != (ROOT / 'e2e/.artifacts').resolve():
                raise ValueError('Unexpected browser artifact cleanup path')
            shutil.rmtree(temporary)


@contextmanager
def deny_dense_generation(fixtures, directory):
    configuration = json.loads((directory / 'storage.json').read_text('utf-8'))
    keys = {channel: configuration['bindings']['search'][channel] for channel in ('bm25', 'dense')}
    schemas = {channel: 'kb_' + SpaceKey(configuration['application_id'], value['owner'], value['space']).digest[:24]
               for channel, value in keys.items()}
    engines = {channel: create_engine(fixtures.selected['dense_postgresql' if channel == 'dense' else 'postgresql']
               .set(drivername='postgresql+psycopg')) for channel in schemas}

    def counts():
        result = {}
        for channel, engine in engines.items():
            with engine.connect() as connection:
                schema = connection.dialect.identifier_preparer.quote(schemas[channel])
                result[channel] = connection.exec_driver_sql('SELECT COUNT(*) FROM ' + schema + '._storage_search_generations').scalar()
        return result

    role = None
    try:
        with engines['dense'].begin() as connection:
            roles = connection.execute(text('SELECT rolname FROM pg_roles WHERE rolname LIKE :prefix '
                'AND has_schema_privilege(oid,:schema,\'USAGE\')'),
                {'prefix': 'dsrun_%', 'schema': schemas['dense']}).scalars().all()
            if len(roles) != 1 or not re.fullmatch(r'dsrun_[0-9a-f]{24}', roles[0]):
                raise ValueError('Expected exactly one controlled dense runtime role')
            role = roles[0]
            quote = connection.dialect.identifier_preparer.quote
            function = quote(schemas['dense']) + '.storage_search_build(text,text,jsonb,integer,text)'
            connection.exec_driver_sql('REVOKE EXECUTE ON FUNCTION ' + function + ' FROM ' + quote(role))
        yield counts
    finally:
        if role is not None:
            with engines['dense'].begin() as connection:
                quote = connection.dialect.identifier_preparer.quote
                function = quote(schemas['dense']) + '.storage_search_build(text,text,jsonb,integer,text)'
                connection.exec_driver_sql('GRANT EXECUTE ON FUNCTION ' + function + ' TO ' + quote(role))
        for engine in engines.values():
            engine.dispose()


def staged_build_id(fixtures, directory, kb_id):
    configuration = json.loads((directory / 'storage.json').read_text('utf-8'))
    value = configuration['bindings']['repository']['primary']
    schema = 'kb_' + SpaceKey(configuration['application_id'], value['owner'], value['space']).digest[:24]
    engine = create_engine(fixtures.selected['postgresql'].set(drivername='postgresql+psycopg'))
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('storage_scope.tenant_id',:tenant,true), set_config('storage_scope.kb_id',:kb,true)"),
                               {'tenant': fixtures.run_id, 'kb': kb_id})
            table = connection.dialect.identifier_preparer.quote(schema) + '.builds'
            rows = connection.execute(text('SELECT build_id FROM ' + table +
                " WHERE kb_id=:kb AND status='STAGING'"), {'kb': kb_id}).scalars().all()
            if len(rows) != 1:
                raise ValueError('Expected one actual failed STAGING build')
            return rows[0]
    finally:
        engine.dispose()


def http_suite(fixtures, *, engines=('postgres', 'embedded', 'sqlite', 'mysql')):
    class HttpStorageIntegration(unittest.TestCase):
        def test_real_rust_http_split_channels_restart_and_failed_build(self):
            for engine in engines:
                with self.subTest(dense_backend=engine), ExitStack() as stack:
                    directory, profile, environment, tokens = http_configuration(fixtures, engine)
                    authentication = (stack.enter_context(redis_http_authentication(fixtures, directory, profile, tokens))
                                      if engine == 'sqlite' else None)
                    owner = {'Authorization': 'Bearer ' + tokens['owner']}
                    denied = {'Authorization': 'Bearer ' + tokens['denied']}
                    other = {'Authorization': 'Bearer ' + tokens['other']}
                    with running_host(directory, environment, profile, 'first') as (client, url):
                        created = client.post('/knowledge-bases', headers=owner, json={'name': 'Synthetic split storage'})
                        self.assertEqual(created.status_code, 201, created.text)
                        kb_id = created.json()['kb_id']
                        policy = {'native_parse': True, 'embedding': True, 'llm_context': True,
                                  'allowed_providers': ['storage_fixture']}
                        uploaded = client.post(f'/knowledge-bases/{kb_id}/sources', headers=owner,
                            files={'file': ('source.txt', TEXT.encode(), 'text/plain')},
                            data={'policy': json.dumps(policy)})
                        self.assertEqual(uploaded.status_code, 201, uploaded.text)
                        source = uploaded.json()
                        self.assertEqual(source['document']['original_sha256'], sha256(TEXT.encode()).hexdigest())
                        staged = client.post(f'/knowledge-bases/{kb_id}/builds', headers=owner, json={'channels': ['hybrid']})
                        self.assertEqual(staged.status_code, 201, staged.text)
                        build = staged.json()
                        self.assertEqual(build['status'], 'VALIDATED')
                        publish = client.post(f'/knowledge-bases/{kb_id}/builds/{build["build_id"]}/publish', headers=owner)
                        self.assertEqual(publish.status_code, 200, publish.text)
                        score = score_http_evidence(client, owner, kb_id, source['source_id'],
                            source['document']['source_version_id'], directory)
                        self.assertEqual(score['decision'], 'PASS', score['rows'])
                        if engine in ('postgres', 'sqlite'):
                            browser = run_browser(directory, environment, url, tokens['owner'])
                            fixtures.observations.setdefault('browser_configurations', []).append({
                                'repository': 'sqlite' if engine == 'sqlite' else 'postgresql', **browser})
                        for channel in ('bm25', 'dense', 'hybrid'):
                            denial = client.post(f'/knowledge-bases/{kb_id}/search', headers=denied,
                                json={'query': 'QUARTZMARKER', 'channel': channel})
                            self.assertEqual(denial.status_code, 403, denial.text)
                            isolation = client.post(f'/knowledge-bases/{kb_id}/search', headers=other,
                                json={'query': 'QUARTZMARKER', 'channel': channel})
                            self.assertEqual(isolation.status_code, 403, isolation.text)
                        member = client.put(f'/knowledge-bases/{kb_id}/members/denied', headers=owner,
                            json={'capabilities': ['kb.read']})
                        self.assertEqual(member.status_code, 200, member.text)
                        for allowed in (False, True, False):
                            readers = client.put(f'/sources/{source["source_id"]}/readers', headers=owner,
                                json={'readers': ['denied'] if allowed else []})
                            self.assertEqual(readers.status_code, 200, readers.text)
                            for channel in ('bm25', 'dense', 'hybrid'):
                                restricted = client.post(f'/knowledge-bases/{kb_id}/search', headers=denied,
                                    json={'query': 'QUARTZMARKER', 'channel': channel, 'top_k': 1})
                                self.assertEqual(restricted.status_code, 200, restricted.text)
                                self.assertEqual(bool(restricted.json()['hits']), allowed)
                                if allowed:
                                    self.assertEqual({hit['source_id'] for hit in restricted.json()['hits']}, {source['source_id']})
                        if engine == 'postgres':
                            with deny_dense_generation(fixtures, directory) as counts:
                                before = counts()
                                failed = client.post(f'/knowledge-bases/{kb_id}/builds', headers=owner,
                                    json={'channels': ['hybrid']})
                                self.assertEqual(failed.status_code, 503, failed.text)
                                after = counts()
                                self.assertEqual(after['bm25'], before['bm25'] + 1)
                                self.assertEqual(after['dense'], before['dense'])
                                failed_build = staged_build_id(fixtures, directory, kb_id)
                            scopes = client.get('/knowledge-bases', headers=owner).json()
                            self.assertEqual(next(row for row in scopes if row['kb_id'] == kb_id)['manifest_id'], build['build_id'])
                            restored = client.post(f'/knowledge-bases/{kb_id}/builds/{failed_build}/resume', headers=owner)
                            self.assertEqual(restored.status_code, 200, restored.text)
                            self.assertEqual(restored.json()['status'], 'VALIDATED')
                            unchanged = client.get('/knowledge-bases', headers=owner).json()
                            self.assertEqual(next(row for row in unchanged if row['kb_id'] == kb_id)['manifest_id'], build['build_id'])
                            recovered = client.post(f'/knowledge-bases/{kb_id}/builds/{failed_build}/publish', headers=owner)
                            self.assertEqual(recovered.status_code, 200, recovered.text)
                            build = {**build, 'build_id': failed_build}
                            fixtures.observations['actual_dense_failure'] = {
                                'failure': 'actual database runtime EXECUTE permission denied',
                                'http_status': failed.status_code, 'generations_before': before,
                                'generations_after': after, 'published_manifest_unchanged': True,
                                'same_build_resumed': failed_build, 'explicit_publication_after_recovery': True}
                        (directory / 'embedding.fail').touch()
                        failed = client.post(f'/knowledge-bases/{kb_id}/builds', headers=owner, json={'channels': ['hybrid']})
                        self.assertIn(failed.status_code, (409, 422, 503), failed.text)
                        (directory / 'embedding.fail').unlink()
                        scopes = client.get('/knowledge-bases', headers=owner).json()
                        self.assertEqual(next(row for row in scopes if row['kb_id'] == kb_id)['manifest_id'], build['build_id'])
                    with running_host(directory, environment, profile, 'restarted') as (client, url):
                        for channel in ('bm25', 'dense', 'hybrid'):
                            response = client.post(f'/knowledge-bases/{kb_id}/search', headers=owner,
                                json={'query': 'QUARTZMARKER', 'channel': channel})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertTrue(response.json()['hits'])
                            self.assertEqual({hit['build_id'] for hit in response.json()['hits']}, {build['build_id']})
                        hit = response.json()['hits'][0]
                        original = client.get(f'/knowledge-bases/{kb_id}/citations/{hit["citation_id"]}/original', headers=owner)
                        self.assertEqual(original.status_code, 200, original.text)
                        self.assertEqual(original.content, TEXT.encode())
                        if authentication is not None:
                            auth, store, issued = authentication
                            self.assertFalse((directory / 'identities.json').exists())
                            self.assertEqual(client.get('/identity', headers=owner).json(),
                                             {'tenant_id': fixtures.run_id, 'principal_id': 'owner'})
                            expired = auth.issue(Actor(fixtures.run_id, 'owner'), ttl_seconds=0.1)
                            issued.append(expired.token)
                            time.sleep(0.15)
                            self.assertIsNone(store.get(auth._key(expired.token)))
                            response = client.get('/identity', headers={'Authorization': 'Bearer ' + expired.token})
                            self.assertEqual(response.status_code, 401, response.text)
                            self.assertEqual(response.headers['WWW-Authenticate'], 'Bearer')
                            corrupt = auth.issue(Actor(fixtures.run_id, 'owner'))
                            issued.append(corrupt.token)
                            store.set(auth._key(corrupt.token), b'not-json')
                            self.assertEqual(client.get('/identity', headers={
                                'Authorization': 'Bearer ' + corrupt.token}).status_code, 503)
                            self.assertTrue(auth.revoke(tokens['owner']))
                            for path in ('/identity', '/knowledge-bases',
                                         f'/knowledge-bases/{kb_id}/citations/{hit["citation_id"]}/original'):
                                response = client.get(path, headers=owner)
                                self.assertEqual(response.status_code, 401, response.text)
                            fixtures.observations['authentication_http'] = {
                                'backend': 'redis', 'identity_file_absent': True, 'browser_authenticated': True,
                                'survived_host_restart': True, 'server_ttl_expired': True,
                                'corrupt_session_status': 503, 'revoked_status': 401,
                                'revoked_original_download_denied': True}
                    fixtures.observations.setdefault('http', []).append({
                        'transport': 'Rust parent + Python worker + actual loopback TCP HTTP',
                        'repository': engine if engine in ('sqlite', 'mysql') else 'postgresql',
                        'bm25': 'postgresql', 'dense': 'postgresql' if engine == 'postgres' else 'embedded', 'kb_id': kb_id, 'source_id': source['source_id'],
                        'originals': 's3' if engine == 'postgres' else 'files', 'original_download_after_restart': True,
                        'build_id': build['build_id'], 'restarted_hits': True, 'failure_did_not_publish': True,
                        'source_acl_grant_and_revoke': 'both channels and hybrid before top-k=1',
                        'evidence_scoring': score['decision'], 'synthetic_evidence_cases': score['total']})

    return unittest.defaultTestLoader.loadTestsFromTestCase(HttpStorageIntegration)


def knowledge_suite(fixtures):
    class KnowledgeStorageIntegration(unittest.TestCase):
        def test_circuit_threshold_uses_old_values_consistently_on_three_actual_engines(self):
            from kb_platform.knowledge_queries import KnowledgeQuery as K
            from kb_platform.knowledge_storage import KnowledgeRepository

            outcomes = []
            for engine in ('sqlite', 'postgresql', 'mysql'):
                _, database = fixtures.structured(engine, 'circuit-old-values', owner='knowledge-circuits')
                repository = KnowledgeRepository(database=database)
                actor, kb = Actor('tenant', 'owner'), 'scope'
                with repository.transaction(actor, kb) as tx:
                    tx.execute(K.CIRCUIT_ENSURE, ('tenant', kb, 'provider'))
                for attempt in (1, 2, 3):
                    with repository.transaction(actor, kb) as tx:
                        tx.execute(K.CIRCUIT_FAILURE, (3, 60, 'tenant', kb, 'provider'))
                        row = tx.execute(K.CIRCUIT_LOCK, ('tenant', kb, 'provider')).fetchone()
                        now = tx.execute(K.JOB_CLOCK).fetchone()['db_now']
                        opened = row['open_until'] is not None and row['open_until'] > now
                        self.assertEqual((row['failures'], opened), (attempt, attempt == 3))
                        outcomes.append({'engine': engine, 'failures': attempt, 'opened': opened})
            fixtures.observations['circuit_threshold'] = outcomes

        def test_legacy_knowledge_transfer(self):
            from .knowledge_transfer_contracts import exercise_legacy_knowledge_transfer

            fixtures.observations['legacy_knowledge_transfer'] = exercise_legacy_knowledge_transfer(self, fixtures)

        def test_actual_job_row_and_circuit_lock_waits_preserve_lease_deadlines(self):
            from kb_platform.knowledge_storage import KnowledgeRepository
            from .knowledge_persistence_contracts import exercise_job_lock_deadlines

            for engine in ('postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    _, database = fixtures.structured(engine, 'job-lock-deadlines', owner='knowledge-jobs')
                    observed = exercise_job_lock_deadlines(self, KnowledgeRepository(database=database))
                    fixtures.observations.setdefault('job_lock_deadlines', []).append({'engine': engine, **observed})

        def test_same_domain_behavior_on_actual_sqlite_postgresql_and_mysql(self):
            from .knowledge_persistence_contracts import exercise_knowledge_repository

            for engine in ('sqlite', 'postgresql', 'mysql'):
                with self.subTest(engine=engine):
                    _, database = fixtures.structured(engine, 'portable-knowledge-' + engine, owner='knowledge-domain')
                    root = fixtures.resource_root / ('portable-knowledge-' + engine)
                    root.mkdir()
                    observed = exercise_knowledge_repository(self, database, root)
                    fixtures.observations.setdefault('portable_knowledge', []).append({'engine': engine, **observed})

        def test_search_restart_preserves_sealed_permissions_and_supports_a_new_build(self):
            import psycopg
            from psycopg import sql

            from kb_platform.database_contracts import DatabaseError
            from kb_platform.database_providers.postgres import pg_transaction
            from .product_fixture import Authority
            from .storage_search_integration import _Configuration

            configuration = _Configuration(fixtures, 'search-restart', 'postgresql', 'postgresql', shared=True)
            initial = configuration.open()
            first = configuration.databases['bm25'].bound
            authority = Authority([('restart.txt', 'RESTARTMARKER evidence stays immutable.')])
            for unit in authority.units:
                unit['vector'] = [1.0, 0.25, 0.125]
            first_index = initial.index_name('tenant', 'scope', '3' * 32)
            initial.stage(first_index, authority.units, vector_dimension=3)
            initial.seal(first_index, 'first-seal')
            denied = []

            def foundation_state(bound):
                with bound._space.migration_engine.connect() as audit:
                    quote = audit.dialect.identifier_preparer.quote
                    prefix = quote(bound.schema) + '.'
                    identity = audit.exec_driver_sql('SELECT * FROM ' + prefix + '_database_identity').mappings().all()
                    migrations = audit.exec_driver_sql('SELECT * FROM ' + prefix +
                        '_database_migrations ORDER BY schema_owner,version').mappings().all()
                    return {'identity': [dict(row) for row in identity], 'migrations': [dict(row) for row in migrations]}

            original_foundation = foundation_state(first)

            def assert_immutable(bound, search, index, phase):
                statements = [('metadata-update', sql.SQL('UPDATE {} SET seal=seal').format(
                    sql.Identifier(bound.schema, '_storage_search_generations')))]
                for table, column in (('_database_identity', 'dataset_id'), ('_database_migrations', 'content_hash')):
                    statements.append((table + '-update', sql.SQL('UPDATE {} SET {}={}').format(
                        sql.Identifier(bound.schema, table), sql.Identifier(column), sql.Identifier(column))))
                for channel, backend in (('bm25', search.bm25), ('dense', search.vector)):
                    target = search.channel_index(index, channel)
                    table = sql.Identifier(bound.schema, backend._generation(target))
                    statements.extend([
                        ('generation-update', sql.SQL('UPDATE {} SET record=record').format(table)),
                        ('generation-delete', sql.SQL('DELETE FROM {}').format(table)),
                        ('generation-insert', sql.SQL('INSERT INTO {} SELECT * FROM {}').format(table, table)),
                    ])
                for operation, statement in statements:
                    with self.subTest(phase=phase, operation=operation):
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            with pg_transaction(bound) as raw:
                                raw.execute(statement)
                        denied.append({'phase': phase, 'operation': operation, 'outcome': 'DENIED'})

            assert_immutable(first, initial, first_index, 'before-restart')
            self.assertEqual(foundation_state(first), original_foundation)
            identity, old_role = first.identity, first.runtime_role
            configuration.close()
            search = configuration.open()
            restored = configuration.databases['bm25'].bound
            self.assertEqual(restored.identity, identity)
            self.assertNotEqual(restored.runtime_role, old_role)
            self.assertEqual(search.read_units(first_index), authority.units)
            self.assertTrue(search.is_sealed(first_index, 'first-seal'))
            assert_immutable(restored, search, first_index, 'after-restart')
            self.assertEqual(foundation_state(restored), original_foundation)
            second_index = search.index_name('tenant', 'scope', '4' * 32)
            search.stage(second_index, authority.units, vector_dimension=3)
            search.seal(second_index, 'second-seal')
            self.assertEqual(search.read_units(second_index), authority.units)
            self.assertTrue(search.is_sealed(second_index, 'second-seal'))
            self.assertTrue(search.is_sealed(first_index, 'first-seal'))
            assert_immutable(restored, search, second_index, 'new-build-after-restart')
            self.assertEqual(foundation_state(restored), original_foundation)
            preconfigured_denials = []
            for table in ('_database_identity', '_database_migrations'):
                limited = PostgresProvider(restored._space.runtime_engine.url,
                    migration_url=restored._space.migration_engine.url, provision=False, schema=restored.schema)
                with restored._space.migration_engine.begin() as audit:
                    quote = audit.dialect.identifier_preparer.quote
                    target = quote(restored.schema) + '.' + quote(table)
                    role = quote(restored.runtime_role)
                    audit.exec_driver_sql('GRANT UPDATE ON ' + target + ' TO ' + role)
                try:
                    with self.assertRaisesRegex(DatabaseError, 'DATABASE_RUNTIME_METADATA_WRITE_PRIVILEGE'):
                        limited.open_space(restored.identity.space)
                    preconfigured_denials.append(table)
                finally:
                    limited.close()
                    with restored._space.migration_engine.begin() as audit:
                        audit.exec_driver_sql('REVOKE UPDATE ON ' + target + ' FROM ' + role)
            self.assertEqual(foundation_state(restored), original_foundation)
            fixtures.observations['search_restart_immutability'] = {
                'dataset_id': identity.dataset_id, 'old_runtime_role': old_role,
                'new_runtime_role': restored.runtime_role, 'first_index': first_index, 'second_index': second_index,
                'old_seal_preserved': True, 'new_build_after_restart': True,
                'foundation_metadata_before': original_foundation, 'foundation_metadata_after': foundation_state(restored),
                'foundation_metadata_unchanged': True, 'unsafe_preconfigured_accounts_rejected': preconfigured_denials,
                'denied_runtime_mutations': denied, 'decision': 'PASS'}

        def test_two_actual_knowledge_spaces_isolate_same_generation_and_rls_connection_reuse(self):
            from kb_platform.knowledge_queries import KnowledgeQuery
            from kb_platform.knowledge_storage import KnowledgeRepository
            from .product_fixture import Authority
            from .storage_search_integration import _Configuration

            clients, repositories, bounds = [], [], []
            for label in ('FIRSTSPACE', 'SECONDSPACE'):
                _, bound = fixtures.bind('postgresql', 'knowledge-' + label, owner='knowledge')
                repository = KnowledgeRepository(database=StructuredSqlSpace(bound._space))
                configuration = _Configuration(fixtures, 'search-' + label, 'postgresql', 'postgresql', shared=True)
                search = configuration.open()
                index = search.index_name('tenant', 'scope', '1' * 32)
                authority = Authority([('same-name.txt', label + ' publication requires review.')])
                for unit in authority.units:
                    unit.update(build_id='1' * 32, vector=[1.0, 0.25, 0.125])
                search.stage(index, authority.units, vector_dimension=3)
                self.assertEqual(search.read_units(index), authority.units)
                self.assertEqual(search.verify(index)['count'], len(authority.units))
                manifest = {'tenant_id': 'tenant', 'kb_id': 'scope', 'build_id': '1' * 32,
                            'index_name': index, 'synthetic_space_label': label}
                search.seal(index, 'seal-' + label)
                search.publish_manifest(manifest, 'seal-' + label)
                self.assertTrue(search.is_sealed(index, 'seal-' + label))
                self.assertEqual(search.current_manifest('tenant', 'scope'), (manifest, 'seal-' + label))
                clients.append((search, authority, label))
                repositories.append(repository)
                bounds.append(bound)
            self.assertNotEqual(bounds[0].physical_name, bounds[1].physical_name)
            for search, authority, label in clients:
                hits = search.search(index, 'tenant', 'scope', authority.rows, label, 1)
                self.assertEqual([hit['text'] for hit in hits], [label + ' publication requires review.'])
                other = 'SECONDSPACE' if label == 'FIRSTSPACE' else 'FIRSTSPACE'
                self.assertEqual(search.search(index, 'tenant', 'scope', authority.rows, other, 1), [])
                self.assertEqual(search.dense(index, 'other-tenant', 'scope', authority.rows, [1., .25, .125], 1), [])

            repository, bound = repositories[0], bounds[0]
            pids = []

            def observe(tx, expected_tenant):
                raw = tx._transaction._transaction
                state = raw.execute("SELECT pg_backend_pid() AS pid, current_setting('storage_scope.tenant_id',true) AS tenant, "
                                    "current_setting('storage_scope.kb_id',true) AS kb").fetchone()
                pids.append(state['pid'])
                self.assertEqual((state['tenant'], state['kb']), (expected_tenant, 'same-kb'))
                return raw

            with repository.transaction(Actor('actor-tenant-A', 'owner'), 'same-kb') as tx:
                raw = observe(tx, 'actor-tenant-A')
                tx.execute(KnowledgeQuery.AUTHORITY_ENSURE, ('actor-tenant-A',))
                tx.execute(KnowledgeQuery.KB_INSERT, ('actor-tenant-A', 'same-kb', 'A-only', 'owner'))
                tx.execute(KnowledgeQuery.MEMBER_INSERT, ('actor-tenant-A', 'same-kb', 'owner', ['kb.read']))
                self.assertEqual([row['name'] for row in raw.execute('SELECT name FROM knowledge_bases').fetchall()], ['A-only'])
            with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
                with repository.transaction(Actor('actor-tenant-B', 'owner'), 'same-kb') as tx:
                    raw = observe(tx, 'actor-tenant-B')
                    self.assertEqual(raw.execute('SELECT name FROM knowledge_bases').fetchall(), [])
                    tx.execute(KnowledgeQuery.AUTHORITY_ENSURE, ('actor-tenant-B',))
                    tx.execute(KnowledgeQuery.KB_INSERT, ('actor-tenant-B', 'same-kb', 'B-rolled-back', 'owner'))
                    raise RuntimeError('synthetic rollback')
            for tenant, expected in (('actor-tenant-A', ['A-only']), ('actor-tenant-B', [])):
                with repository.transaction(Actor(tenant, 'owner'), 'same-kb') as tx:
                    raw = observe(tx, tenant)
                    self.assertEqual([row['name'] for row in raw.execute('SELECT name FROM knowledge_bases').fetchall()], expected)
            with bound.transaction(write=False) as tx:
                state = tx.execute("SELECT pg_backend_pid() AS pid, current_setting('storage_scope.tenant_id',true) AS tenant, "
                                   "current_setting('storage_scope.kb_id',true) AS kb").fetchone()
                pids.append(state['pid'])
                self.assertFalse(state['tenant'])
                self.assertFalse(state['kb'])
                self.assertEqual(tx.execute('SELECT COUNT(*) AS count FROM knowledge_bases').fetchone()['count'], 0)
            self.assertEqual(len(set(pids)), 1, 'actor transactions must reuse the same actual physical connection')
            fixtures.observations['knowledge_isolation'] = {
                'same_index': index, 'same_tenant_kb_and_build': True,
                'namespaces': [bound.physical_name for bound in bounds],
                'independent_records_manifests_and_seals': True,
                'physical_backend_pid': pids[0], 'transactions_on_same_connection': len(pids),
                'actor_switch_and_exception_rollback': 'RLS and transaction-local settings verified'}

    return unittest.defaultTestLoader.loadTestsFromTestCase(KnowledgeStorageIntegration)


def planned_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from planned_tests(test)
        else:
            yield test.id()


def run(output, environment=None):
    environment = os.environ if environment is None else environment
    selected = configuration(environment)
    output.mkdir(parents=True, exist_ok=False)
    observations = {}
    fixtures = StorageFixtures(selected, output, observations, environment)
    report = {'status': 'FAIL', 'new_model_calls': 0, 'new_embedding_calls': 0,
              'new_judge_calls': 0, 'scope': 'actual six-category storage and Rust HTTP/browser flows; synthetic evidence controls',
              'planned': {'session_engines': 3, 'cross_engine_clones': 6, 'persistence_engines': 3,
                          'transaction_atomicity_probes': 78, 'nontransactional_schema_rejections': 1,
                          'knowledge_namespaces': 2, 'physical_connection_reuse': 1,
                          'http_configurations': 4, 'synthetic_evidence_cases': 12, 'browser_cases': 2},
              'backends': {engine: {'host': url.host, 'port': url.port, 'database': url.database}
                           for engine, url in selected.items() if engine != 's3_endpoint'}}
    try:
        with (output / 'checks.log').open('w', encoding='utf-8') as stream:
            from .storage_contract_integration import suite as contract_suite
            from .storage_search_integration import suite as search_contract_suite
            from .storage_object_integration import suite as object_contract_suite
            from .model_provider_integration import suite as model_provider_suite
            from .build_recovery_contracts import live_suite as build_recovery_suite

            suite = unittest.TestSuite((foundation_suite(fixtures), session_suite(fixtures),
                                        knowledge_suite(fixtures), contract_suite(fixtures),
                                        search_contract_suite(fixtures), object_contract_suite(fixtures),
                                        http_suite(fixtures), model_provider_suite(), build_recovery_suite(fixtures)))
            planned = tuple(planned_tests(suite))
            write_json(output / 'plan.json', {'test_ids': planned, 'total': len(planned),
                       'new_model_calls_limit': 0, 'new_embedding_calls_limit': 0, 'new_judge_calls_limit': 0})
            report['planned']['test_groups'] = len(planned)
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        report.update({'tests': result.testsRun, 'failures': len(result.failures),
                       'errors': len(result.errors), 'skipped': len(result.skipped),
                       'expected_failures': len(result.expectedFailures),
                       'not_run': len(planned) - result.testsRun,
                       'failure_details': [{'test': test.id(), 'traceback': detail}
                                           for test, detail in (*result.failures, *result.errors)]})
        if passed_contract_run(result, len(planned)):
            report['status'] = 'PASS'
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        try:
            fixtures.close()
        except Exception as error:
            report['cleanup_error'] = str(error)
            report['status'] = 'FAIL'
        report['observations'] = observations
        write_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description='Run controlled actual storage integration with no external model calls.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.output.resolve())
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({key: report.get(key) for key in ('status', 'tests', 'failures', 'errors', 'skipped')}))
    raise SystemExit(0 if report['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
