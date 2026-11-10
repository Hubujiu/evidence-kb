from contextlib import nullcontext
from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_platform.application_composition import create_root_application
from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.documents import parse_native_document
from kb_platform.platform_ports import KnowledgeRepository
from kb_platform.database_contracts import SpaceKey, StorageIdentity
from kb_platform.knowledge_queries import KnowledgeQuery
from kb_platform.knowledge_storage import KNOWLEDGE_TRANSACTION_LIMITS
from kb_platform.plugin_host import PluginSpec
from kb_platform.settings import Settings


class PlatformPluginContracts(unittest.TestCase):
    def settings(self, root):
        return Settings(Path(root), Path(root) / 'identities.json')

    def test_constructor_uses_only_explicit_ports_and_never_reads_environment(self):
        repository = SimpleNamespace(dsn='fixture', transaction=lambda *args: nullcontext('connection'))
        ports = dict(repository=repository, search=object(), store=object(), parser=object(),
                     embedding_jobs=object(), embedding_provider=object(), ocr_client=object())
        with patch('os.getenv', side_effect=AssertionError('constructor configuration must be explicit')):
            platform = Platform(**ports)
        self.assertIs(platform.repository, repository)
        self.assertFalse(hasattr(platform, 'dsn'))
        self.assertIs(platform.backend, ports['search'])
        self.assertIs(platform.store, ports['store'])
        self.assertIs(platform.parser, ports['parser'])
        self.assertIs(platform.embedding_jobs, ports['embedding_jobs'])
        with platform._tx(Actor('tenant', 'reader'), 'scope') as connection:
            self.assertEqual(connection, 'connection')

    def test_repository_preserves_trusted_scope_and_borrows_storage_lifecycle(self):
        connection = Mock()
        database = SimpleNamespace(
            identity=StorageIdentity(SpaceKey('app', 'knowledge', 'content'), 'dataset'),
            prepare=Mock(), seal=Mock(), transaction=Mock(return_value=nullcontext(connection)), close=Mock())
        repository = KnowledgeRepository(database=database)
        with repository.transaction(Actor('tenant', 'reader'), 'scope') as current:
            self.assertIsNot(current, connection)
        database.transaction.assert_called_once_with(write=True, limits=KNOWLEDGE_TRANSACTION_LIMITS,
                                                     scope={'tenant_id': 'tenant', 'kb_id': 'scope'})
        self.assertEqual(database.prepare.call_args.args[0].owner, 'knowledge')
        database.seal.assert_called_once_with()
        connection.execute.assert_not_called()
        database.close.assert_not_called()

    def test_ingest_checks_authority_before_parser_and_rechecks_after_parser(self):
        data = b'Synthetic source with an immutable version.'
        digest = hashlib.sha256(data).hexdigest()
        events = []
        repository = SimpleNamespace(dsn='', transaction=lambda *args: nullcontext(Mock()))
        parser = Mock(side_effect=lambda *args: (events.append('parse'),
                      parse_native_document(*args))[1])
        platform = Platform(repository=repository, search=object(),
            store=SimpleNamespace(put=lambda *args: ('original', digest)), parser=parser,
            embedding_jobs=object())
        platform._revision = lambda *args: 1
        platform._lock_authority = lambda *args: events.append('lock')

        def authorize(*args):
            events.append('authorize')
            if len([event for event in events if event == 'authorize']) == 2:
                raise PlatformError('ACCESS_DENIED', 'revoked', 403)

        platform._auth = authorize
        with self.assertRaisesRegex(PlatformError, 'revoked'):
            platform.ingest(Actor('tenant', 'writer'), 'scope', 'fixture.md', data)
        self.assertEqual(events, ['authorize', 'parse', 'lock', 'authorize'])
        self.assertEqual(parser.call_args.args[2]['_parse_authorization'],
                         {'principal': 'tenant:writer', 'acl_revision': '1'})

    def test_two_selected_search_implementations_serve_same_platform_consumer(self):
        from kb_platform.embedded_search import EmbeddedSearch
        from kb_platform.search import OpenSearch
        from .product_fixture import Authority

        authority = Authority([('fixture.md', 'Synthetic immutable rule.')])
        source = next(unit for unit in authority.units if unit['kind'] != 'Page')
        record = authority.records[source['citation_id']]
        with TemporaryDirectory() as root:
            settings = self.settings(root)
            expected = None
            for label in ('synthetic-local', 'synthetic-service'):
                closed = []
                backend = EmbeddedSearch(Path(root) / 'search') if label == 'synthetic-local' else OpenSearch('https://synthetic.invalid')
                index = backend.index_name('tenant', 'scope', '1' * 32)
                if label == 'synthetic-local':
                    backend.stage(index, authority.units)
                else:
                    backend._request = Mock(return_value={'hits': {'hits': [
                        {'_source': deepcopy(source), '_score': 1.0}]}})

                def mount(ctx, config):
                    if label == 'synthetic-local':
                        ctx.defer(backend.close)
                    ctx.defer(lambda: closed.append(label))
                    ctx.provide('platform.search', backend)

                resources = ('platform.repository', 'platform.originals', 'platform.parser',
                             'platform.embedding_jobs', 'platform.embedding', 'platform.ocr')

                def provide_resources(ctx, config):
                    for name in resources:
                        ctx.provide(name, None if name.endswith(('embedding', 'ocr')) else object())

                selected = {'version': 1, 'plugins': [
                    {'id': 'resources', 'use': 'fixture.resources'}, {'id': 'search', 'use': label},
                    {'id': 'platform', 'use': 'platform.service'}]}
                composition = create_root_application(settings, profile=selected, environment={},
                    extra_plugins=(PluginSpec(label, ('platform.search',), (), mount),
                                   PluginSpec('fixture.resources', resources, (), provide_resources)))
                self.addCleanup(composition.close)
                platform = composition.host.service('platform')
                connection = Mock()
                build = {'build_id': 'build', 'index_name': index, 'inputs': {'channels': ['bm25']}}
                connection.execute.side_effect = lambda sql, *args: SimpleNamespace(
                    fetchone=lambda: record if sql == KnowledgeQuery.CITATION_GET else build)
                platform.repository = SimpleNamespace(transaction=lambda *args: nullcontext(connection))
                platform._auth = lambda *args: {'manifest_id': 'build', 'revision': 1}
                platform._revision = lambda *args: 1
                platform._lock_authority = lambda *args: 1
                platform._source_snapshot = authority._source_snapshot
                result = composition.platform.search(authority.actor, 'scope', 'rule')
                projection = {key: result[key] for key in ('context', 'manifest_id', 'channel', 'trace')}
                if expected is None:
                    expected = deepcopy(projection)
                self.assertEqual(projection, expected)
                self.assertEqual(result['context'], 'Synthetic immutable rule.')
                self.assertEqual([hit['citation_id'] for hit in result['hits']], [source['citation_id']])
                composition.close()
                self.assertEqual(closed, [label])

    def test_native_profile_and_borrowed_platform_preserve_resource_ownership(self):
        with TemporaryDirectory() as root:
            settings = self.settings(root)
            settings = Settings(settings.storage_root, settings.identities_file, Path(root) / 'missing-storage.json')
            native = create_root_application(settings,
                profile={'version': 1, 'plugins': [{'id': 'runtime', 'use': 'harness.runtime'}]},
                environment={})
            self.assertIsNone(native.platform)
            self.assertIsNone(native.application)
            native.close()
            resource = SimpleNamespace(embedding_provider=None, ocr_client=None, close=Mock())
            borrowed = create_root_application(settings, platform=resource, environment={})
            self.assertIs(borrowed.host.service('platform'), resource)
            borrowed.close()
            resource.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
