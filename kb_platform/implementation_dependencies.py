from importlib.metadata import version
from hashlib import sha256
import json
from pathlib import Path
from platform import python_version


ROOT = Path(__file__).resolve().parents[1]


def implementation_dependencies(*files, packages=()):
    if not files:
        raise ValueError('IMPLEMENTATION_DEPENDENCY_FILES_REQUIRED')
    paths = [Path(__file__).resolve(), *(Path(path).resolve() for path in files)]
    keys = [path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name for path in paths]
    if len(keys) != len(set(keys)):
        raise ValueError('IMPLEMENTATION_DEPENDENCY_FILES_REQUIRED')
    return {'files': {key: sha256(path.read_bytes()).hexdigest() for key, path in zip(keys, paths, strict=True)},
            'packages': {name: version(name) for name in packages}, 'python': python_version()}


def dependency_manifest(value):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or not isinstance(value.get('implementation'), dict) or not value['implementation']
            or not isinstance(value.get('storage'), dict) or not value['storage']):
        raise ValueError('DEPENDENCY_MANIFEST_REQUIRED')
    return json.loads(json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False))
