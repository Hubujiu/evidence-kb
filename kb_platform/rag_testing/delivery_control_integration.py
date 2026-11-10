import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DeliveryControlIntegration(unittest.TestCase):
    def run_fixture(self, case):
        result = subprocess.run([sys.executable, '-B', '-m', 'kb_platform.rag_testing.delivery_control_fixture', case],
                                cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=25, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unpublished_failures_never_execute_host(self):
        self.run_fixture('startup_failures')

    def test_post_release_interrupt_preserves_published_owner(self):
        self.run_fixture('post_release_interrupt')

    def test_existing_stop_marker_cannot_modify_hard_link_target(self):
        self.run_fixture('stop_marker')

    def test_same_profile_with_wrong_instance_is_not_ready(self):
        self.run_fixture('wrong_instance')
