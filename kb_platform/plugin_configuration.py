from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path


def config_schema(properties=None, *, required=(), **constraints):
    return {'type': 'object', 'properties': {} if properties is None else properties,
            'additionalProperties': False, 'required': list(required), **constraints}


def configured_plugins(specs, schemas=None, checks=None, inputs=None):
    schemas = {} if schemas is None else schemas
    checks = {} if checks is None else checks
    inputs = {} if inputs is None else inputs
    return tuple(replace(spec, config_schema=schemas.get(spec.key, config_schema()),
                         config_check=checks.get(spec.key), config_inputs=inputs.get(spec.key)) for spec in specs)


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
