"""Expand explicitly supplied bundles before the native host validates services.

Bundles contain configuration, never import paths or executable expressions.
Profile rows override one bundle row by id; their config replaces the whole
config object. Expansion does not mount plugins or acquire resources.
"""
from copy import deepcopy
import json
from pathlib import Path

from .plugin_native import PluginError


def read_profile(value):
    return deepcopy(value) if isinstance(value, dict) else json.loads(Path(value).read_text('utf-8'))


def expand_profile(value, bundles=None):
    catalog = {} if bundles is None else bundles

    def expand(document, stack):
        if (not isinstance(document, dict) or set(document) - {'version', 'bundles', 'plugins'}
                or type(document.get('version')) is not int or document['version'] != 1
                or not isinstance(document.get('plugins', []), list)
                or not isinstance(document.get('bundles', []), list)):
            raise PluginError('INVALID_PLUGIN_PROFILE')
        rows = {}
        selected = set()
        for key in document.get('bundles', []):
            if not isinstance(key, str) or key not in catalog:
                raise PluginError('UNKNOWN_PLUGIN_BUNDLE', str(key))
            if key in stack:
                raise PluginError('CYCLIC_PLUGIN_BUNDLE', key)
            if key in selected:
                raise PluginError('DUPLICATE_PLUGIN_BUNDLE', key)
            selected.add(key)
            for row in expand(read_profile(catalog[key]), (*stack, key)):
                if row['id'] in rows:
                    raise PluginError('DUPLICATE_BUNDLE_PLUGIN_ID', row['id'])
                rows[row['id']] = row
        seen = set()
        for row in document.get('plugins', []):
            if (not isinstance(row, dict) or set(row) - {'id', 'use', 'config', 'overrides', 'disabled'}
                    or not isinstance(row.get('id'), str) or not row['id']
                    or type(row.get('disabled', False)) is not bool
                    or not isinstance(row.get('config', {}), dict)):
                raise PluginError('INVALID_PLUGIN_ENTRY')
            key = row['id']
            if key in seen:
                raise PluginError('DUPLICATE_PLUGIN_ID', key)
            seen.add(key)
            merged = {**rows.get(key, {}), **deepcopy(row)}
            if not isinstance(merged.get('use'), str) or not merged['use']:
                raise PluginError('INVALID_PLUGIN_ENTRY', key)
            rows[key] = merged
        return list(rows.values())

    return {'version': 1, 'plugins': expand(read_profile(value), ())}
