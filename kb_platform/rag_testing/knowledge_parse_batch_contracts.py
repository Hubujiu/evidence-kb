from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kb_platform.core import Actor, Platform, PlatformError
from kb_platform.database_contracts import DatabaseError, QueryLimits
from kb_platform.knowledge_queries import KnowledgeQuery as K
from kb_platform.knowledge_schema import knowledge_schema
from kb_platform.knowledge_storage import KnowledgeRepository
from .storage_structured_wire_contracts import structured


class KnowledgeParseBatchContracts(unittest.TestCase):
    def fixture(self, root, client, size=10):
        repository = KnowledgeRepository(database=client)
        actor = Actor('tenant', 'owner')
        platform = Platform(repository=repository, search=None, store=None, parser=None, embedding_jobs=None)
        scope = platform.create_kb(actor, 'Exact parse identities')['kb_id']
        identities = [('a', 'v1', 'p1'), ('a', 'v2', 'p2'), ('b', 'v1', 'p2')]
        with repository.transaction(actor, scope) as conn:
            for source in ('a', 'b'):
                conn.execute(K.SOURCE_INSERT, (actor.tenant_id, source, actor.principal_id, [], {}))
            for source, version, snapshot in identities:
                conn.execute(K.SOURCE_VERSION_INSERT,
                    (actor.tenant_id, source, version, source, str(Path(root) / source), '0' * 64, 'text/plain'))
                conn.execute(K.PARSE_INSERT, (actor.tenant_id, source, version, snapshot,
                                              {'text': source + version + snapshot + 'x' * size}))
        return repository, actor, scope, identities

    def test_wire_reads_exact_identity_pairs_and_rejects_missing_duplicates_and_tenant_change(self):
        with TemporaryDirectory() as root, structured(root, knowledge_schema('knowledge')) as (client, _, _):
            repository, actor, scope, identities = self.fixture(root, client)
            found = list(repository.parse_documents(actor, scope, identities[::-1]))
            self.assertEqual([item[0] for item in found], identities[::-1])
            for identity, document in found:
                self.assertTrue(document['text'].startswith(''.join(identity)))
            with self.assertRaises(PlatformError) as missing:
                list(repository.parse_documents(actor, scope, [('a', 'v1', 'p2')]))
            self.assertEqual(missing.exception.code, 'SOURCE_INTEGRITY')
            with self.assertRaises(ValueError):
                list(repository.parse_documents(actor, scope, [identities[0], identities[0]]))
            with repository.transaction(actor, scope, write=False) as conn:
                with self.assertRaises(PlatformError):
                    conn.execute(K.PARSE_DOCUMENTS, ('other-tenant', identities))

    def test_wire_result_limit_rolls_back_before_smaller_reads_and_keeps_single_item_failure(self):
        with TemporaryDirectory() as root, structured(root, knowledge_schema('knowledge')) as (client, _, messages):
            repository, actor, scope, identities = self.fixture(root, client, size=1100)
            import json
            messages.clear()
            with patch('kb_platform.knowledge_storage.KNOWLEDGE_TRANSACTION_LIMITS',
                       QueryLimits(max_bytes=2400)):
                found = list(repository.parse_documents(actor, scope, identities))
            self.assertEqual([identity for identity, _ in found], identities)
            finishes = [json.loads(message) for message in messages if '"finish"' in message]
            self.assertTrue(any(row.get('commit') is False for row in finishes))
            with patch('kb_platform.knowledge_storage.KNOWLEDGE_TRANSACTION_LIMITS',
                       QueryLimits(max_bytes=500)):
                with self.assertRaises(DatabaseError) as oversized:
                    list(repository.parse_documents(actor, scope, identities[:1]))
            self.assertEqual(oversized.exception.code, 'DATABASE_RESULT_LIMIT')
