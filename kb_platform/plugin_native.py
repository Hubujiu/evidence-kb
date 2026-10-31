import ctypes
from functools import lru_cache
import json
import os

from .agent_native import library_path


class PluginError(RuntimeError):
    def __init__(self, code, message=''):
        self.code = code
        super().__init__(message or code)


@lru_cache(maxsize=4)
def _library(path):
    try:
        library = ctypes.CDLL(str(path))
        library.agent_harness_call.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        library.agent_harness_call.restype = ctypes.c_void_p
        library.agent_harness_free.argtypes = [ctypes.c_void_p]
        library.agent_harness_free.restype = None
        return library
    except (OSError, AttributeError) as exc:
        raise PluginError('PLUGIN_NATIVE_UNAVAILABLE') from exc


@lru_cache(maxsize=4)
def _selected_library(configured):
    return _library(library_path().resolve())


def native_call(command, **fields):
    if command.startswith('runtime_'):
        fields = {'protocol': 'plugin-runtime/1', **fields}
    payload = json.dumps({'version': 1, 'command': command, **fields}, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode('utf-8')
    library = _selected_library(os.environ.get('KB_AGENT_KERNEL_LIBRARY'))
    pointer = library.agent_harness_call(payload, len(payload))
    if not pointer:
        raise PluginError('PLUGIN_NATIVE_EMPTY_RESPONSE')
    try:
        result = json.loads(ctypes.string_at(pointer))
    finally:
        library.agent_harness_free(pointer)
    if not result['ok']:
        raise PluginError(result['error']['code'], result['error']['message'])
    return result['result']
