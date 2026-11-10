import unittest

from kb_platform.knowledge_parse_cache import ImmutableParseCache


class KnowledgeParseCacheContracts(unittest.TestCase):
    def test_exact_identity_and_independent_values(self):
        cache = ImmutableParseCache()
        key = ('dataset', 'tenant', 'source', 'version', 'parse')
        original = {'elements': [{'text': 'original'}]}
        cache.put(key, original)
        original['elements'][0]['text'] = 'mutated'
        returned = cache.get(key)
        self.assertEqual(returned['elements'][0]['text'], 'original')
        returned['elements'].clear()
        self.assertEqual(cache.get(key)['elements'][0]['text'], 'original')
        for position in range(len(key)):
            changed = list(key)
            changed[position] += '-changed'
            self.assertIsNone(cache.get(tuple(changed)))

    def test_capacity_and_oversized_documents_do_not_displace_valid_items(self):
        cache = ImmutableParseCache(max_bytes=24, max_entries=2)
        cache.put('a', {'a': 1})
        cache.put('b', {'b': 2})
        cache.get('a')
        cache.put('c', {'c': 3})
        self.assertIsNone(cache.get('b'))
        self.assertEqual(cache.get('a'), {'a': 1})
        cache.put('large', {'text': 'x' * 24})
        self.assertIsNone(cache.get('large'))
        self.assertEqual(cache.get('a'), {'a': 1})
        cache.put('d', {'long-key': 4})
        self.assertIsNone(cache.get('c'))

    def test_corrupt_cached_bytes_are_not_returned(self):
        cache = ImmutableParseCache()
        cache.put('key', {'text': 'verified'})
        raw, checksum = cache._entries['key']
        cache._entries['key'] = (raw.replace(b'verified', b'altered!'), checksum)
        with self.assertRaisesRegex(ValueError, 'IMMUTABLE_PARSE_CACHE_INTEGRITY'):
            cache.get('key')
