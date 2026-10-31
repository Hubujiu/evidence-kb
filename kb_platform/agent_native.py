import ctypes
from functools import lru_cache
import os
from pathlib import Path
import sys


ABI_VERSION = 1
PROTOCOL = 'rust-agent-kernel/1'


class KernelError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _State(ctypes.Structure):
    _fields_ = [('abi_version', ctypes.c_uint32), ('phase', ctypes.c_uint32),
                ('pending_kind', ctypes.c_uint32), ('reserved', ctypes.c_uint32),
                ('max_operations', ctypes.c_uint64), ('operations_started', ctypes.c_uint64),
                ('pending_id', ctypes.c_uint64), ('deadline_ms', ctypes.c_uint64),
                ('last_now_ms', ctypes.c_uint64)]


class _Event(ctypes.Structure):
    _fields_ = [('kind', ctypes.c_uint32), ('operation_kind', ctypes.c_uint32),
                ('operation_id', ctypes.c_uint64), ('now_ms', ctypes.c_uint64)]


class _Transition(ctypes.Structure):
    _fields_ = [('state', _State), ('status', ctypes.c_uint32), ('reserved', ctypes.c_uint32)]


def library_path():
    configured = os.environ.get('KB_AGENT_KERNEL_LIBRARY')
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise KernelError('AGENT_KERNEL_PATH_MUST_BE_ABSOLUTE')
        return path
    filename = ('agent_kernel.dll' if sys.platform == 'win32' else
                'libagent_kernel.dylib' if sys.platform == 'darwin' else 'libagent_kernel.so')
    if os.environ.get('PLUGIN_HOST_EXECUTABLE'):
        return Path(os.environ['PLUGIN_HOST_EXECUTABLE']).parent / filename
    return Path(__file__).resolve().parents[1] / 'target' / 'debug' / filename


@lru_cache(maxsize=4)
def _load(path):
    try:
        library = ctypes.CDLL(str(path))
        library.agent_kernel_abi_version.argtypes = []
        library.agent_kernel_abi_version.restype = ctypes.c_uint32
        if library.agent_kernel_abi_version() != ABI_VERSION:
            raise KernelError('AGENT_KERNEL_ABI_MISMATCH')
        library.agent_kernel_new.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
        library.agent_kernel_new.restype = _Transition
        library.agent_kernel_apply.argtypes = [_State, _Event]
        library.agent_kernel_apply.restype = _Transition
        return library
    except (OSError, AttributeError) as exc:
        raise KernelError('AGENT_KERNEL_UNAVAILABLE') from exc


class Kernel:
    def __init__(self, max_operations, deadline_ms):
        for value in (max_operations, deadline_ms):
            if type(value) is not int or not 0 < value < 2**64:
                raise ValueError('INVALID_RUNTIME_LIMIT')
        self._library = _load(library_path().resolve())
        self._receive(self._library.agent_kernel_new(max_operations, deadline_ms))

    def _receive(self, result):
        self._state = result.state
        if result.status:
            code = {11: 'AGENT_DEADLINE_EXCEEDED', 12: 'AGENT_OPERATION_LIMIT'}.get(
                result.status, f'AGENT_KERNEL_REJECTED_{result.status}')
            raise KernelError(code)

    def apply(self, event, *, operation_id=0, operation_kind='none', elapsed_ms=0):
        if type(operation_id) is not int or not 0 <= operation_id < 2**64:
            raise ValueError('INVALID_OPERATION_ID')
        if type(elapsed_ms) is not int or not 0 <= elapsed_ms < 2**64:
            raise ValueError('INVALID_ELAPSED_TIME')
        kinds = {'none': 0, 'model': 1, 'tool': 2, 'host': 3}
        events = {'dispatch': 1, 'settle': 2, 'finish': 3, 'cancel': 4, 'fail': 5}
        self._receive(self._library.agent_kernel_apply(self._state,
            _Event(events[event], kinds[operation_kind], operation_id, elapsed_ms)))

    @property
    def phase(self):
        return ('ready', 'waiting', 'completed', 'cancelled', 'failed', 'expired', 'limit')[self._state.phase]

    @property
    def operations_started(self):
        return self._state.operations_started
