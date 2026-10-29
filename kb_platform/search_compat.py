"""Narrow compatibility for a reviewed query-only PostgreSQL fix."""
from copy import deepcopy


_POSTGRES_BEFORE = frozenset({
    'ce616199541d736792c7cf8e1d8b73b439eca5eaeeb868c00e495b5c5a745ae8',
    'f2e20ae03866a1386f23ee3a878d7cb148d4b052c8d6ed865eae3e190a248427',
})
_POSTGRES_AFTER = frozenset({
    '02cf199bc551b9986363d96ddb5a7da9bd009f010485d30a1b549d1f5bc3e31b',
    'a646deb614df36a8e08de8b4b66a17b49c8dbfb3b9828ca4eafee2166b5fb233',
})


def resumable_search_dependencies(frozen, current):
    if frozen == current:
        return True
    if not isinstance(frozen, dict) or not isinstance(current, dict):
        return False
    before, after = deepcopy(frozen), deepcopy(current)
    try:
        old = before['files'].pop('postgres_search.py')
        new = after['files'].pop('postgres_search.py')
    except (KeyError, TypeError, AttributeError):
        return False
    return old in _POSTGRES_BEFORE and new in _POSTGRES_AFTER and before == after
