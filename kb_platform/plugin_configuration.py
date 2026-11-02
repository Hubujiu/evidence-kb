from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path


def valid_plugin_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 128 and value.isascii()
            and all(character.isalnum() or character in '._:/-' for character in value))


def config_schema(properties=None, *, required=(), **constraints):
    return {'type': 'object', 'properties': {} if properties is None else properties,
            'additionalProperties': False, 'required': list(required), **constraints}


def configured_plugins(specs, schemas=None, checks=None, inputs=None):
    specs = tuple(specs)
    keys = {spec.key for spec in specs}
    overrides = {'config_schema': schemas, 'config_check': checks, 'config_inputs': inputs}
    for field, values in overrides.items():
        if values is not None and (not isinstance(values, Mapping) or set(values) - keys):
            raise ValueError(f'INVALID_PLUGIN_CONFIGURATION: {field}')
    return tuple(replace(spec, **{field: values[spec.key] for field, values in overrides.items()
                                if values is not None and spec.key in values}) for spec in specs)


def file_inputs(paths):
    return {str(path.resolve()): sha256(path.read_bytes()).hexdigest() for path in map(Path, paths)}


def environment_inputs(names):
    return {'env:' + name: sha256(os.environ.get(name, '').encode('utf-8')).hexdigest() for name in names}


TEXT = {'type': 'string', 'minLength': 1}
POSITIVE = {'type': 'integer', 'minimum': 1}
CONTEXT_PROPERTIES = {'input_budget': POSITIVE, 'output_reserve': POSITIVE,
                      'model_window': POSITIVE, 'safety_reserve': {'type': 'integer', 'minimum': 0},
                      'strict_tokens': {'type': 'boolean'}}


def check_context(config):
    from .context_budget import ContextBudget

    ContextBudget(**config)
