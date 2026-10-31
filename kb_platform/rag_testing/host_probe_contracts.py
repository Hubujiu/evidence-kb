import errno
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import host_integration


class HostProcessProbeContracts(unittest.TestCase):
    def probe(self, state):
        return (
            patch.object(host_integration, 'os', SimpleNamespace(name='posix', kill=Mock())),
            patch.object(host_integration.sys, 'platform', 'linux'),
            patch.object(host_integration, 'Path', return_value=state),
        )

    def test_process_exiting_during_linux_stat_read_is_not_running(self):
        for error in (FileNotFoundError(errno.ENOENT, 'No such file'),
                      ProcessLookupError(errno.ESRCH, 'No such process')):
            state = Mock()
            state.read_text.side_effect = error
            operating_system, platform, path = self.probe(state)
            with self.subTest(error=type(error).__name__), operating_system, platform, path:
                self.assertFalse(host_integration.process_running(123))

    def test_linux_probe_distinguishes_live_zombie_and_unreadable_process(self):
        state = Mock()
        operating_system, platform, path = self.probe(state)
        with operating_system, platform, path:
            for status, expected in (('S', True), ('Z', False)):
                state.read_text.return_value = f'123 (worker) {status} 1 2 3'
                with self.subTest(status=status):
                    self.assertEqual(expected, host_integration.process_running(123))
            state.read_text.side_effect = PermissionError(errno.EACCES, 'Permission denied')
            with self.assertRaises(PermissionError):
                host_integration.process_running(123)
