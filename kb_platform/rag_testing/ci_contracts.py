from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from .browser_integration import CASES, acceptance_dsn, browser_verdict, checked_native
from scripts.check_project import workflow_violations


ROOT = Path(__file__).resolve().parents[2]


class CiContracts(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text('utf-8'))

    def test_current_workflow_requires_database_and_browser_acceptance(self):
        self.assertEqual([], workflow_violations(self.workflow))

    def test_optional_or_missing_integration_is_rejected(self):
        variants = []
        missing = deepcopy(self.workflow)
        missing['jobs']['required']['needs'].remove('integration')
        variants.append(missing)
        for key, value in (('if', 'false'), ('continue-on-error', True)):
            optional = deepcopy(self.workflow)
            optional['jobs']['integration'][key] = value
            variants.append(optional)
            optional_aggregate = deepcopy(self.workflow)
            optional_aggregate['jobs']['required'][key] = value
            variants.append(optional_aggregate)
        for workflow in variants:
            with self.subTest(workflow=workflow['jobs']['integration'].get('if')):
                self.assertTrue(workflow_violations(workflow))

    def test_missing_skipped_or_ignored_gate_is_rejected(self):
        for target in ('scripts/check_project.py', 'kb_platform.rag_testing.browser_integration'):
            for mutation in ('delete', 'skip', 'ignore'):
                changed = deepcopy(self.workflow)
                steps = changed['jobs']['integration']['steps']
                step = next(step for step in steps if target in step.get('run', ''))
                if mutation == 'delete':
                    steps.remove(step)
                elif mutation == 'skip':
                    step['if'] = 'false'
                else:
                    step['continue-on-error'] = True
                with self.subTest(target=target, mutation=mutation):
                    self.assertTrue(workflow_violations(changed))

    def test_required_cannot_ignore_integration_result(self):
        changed = deepcopy(self.workflow)
        changed['jobs']['required']['steps'][0]['run'] = 'true'
        self.assertTrue(workflow_violations(changed))

    def test_required_cannot_replace_quality_or_secrets_with_literal_success(self):
        changed = deepcopy(self.workflow)
        changed['jobs']['required']['needs'] = ['integration']
        changed['jobs']['required']['steps'][0]['env'].update(
            QUALITY_RESULT='success', SECRETS_RESULT='success')
        self.assertTrue(workflow_violations(changed))

    def test_browser_database_is_dedicated_and_unprivileged(self):
        valid = 'postgresql://kb_app:password@127.0.0.1:55432/rust_plugin_acceptance_ci'
        self.assertEqual('rust_plugin_acceptance_ci', acceptance_dsn(valid))
        for invalid in (valid.replace('kb_app', 'kb_admin'), valid.replace('127.0.0.1', 'example.com'),
                        valid.replace('rust_plugin_acceptance_ci', 'evidence_kb'),
                        valid + '?hostaddr=203.0.113.1'):
            with self.subTest(dsn=invalid), self.assertRaises(ValueError):
                acceptance_dsn(invalid)

    def test_empty_skipped_flaky_or_failed_browser_run_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'junit.xml'
            cases = [f'<testcase name="{name}" classname="knowledge.e2e.js">{{}}</testcase>' for name in sorted(CASES)]
            valid = ''.join(case.format('') for case in cases)
            report.write_text('<testsuite>' + valid + '</testsuite>', 'utf-8')
            self.assertEqual(2, browser_verdict(report)['tests'])
            for inner in ('', cases[0].format(''), cases[0].format('') * 2,
                          *(cases[0].format('') + cases[1].format(tag) for tag in
                            ('<skipped/>', '<failure/>', '<error/>', '<system-out>flaky: 1 failed attempt</system-out>'))):
                report.write_text('<testsuite>' + inner + '</testsuite>', 'utf-8')
                with self.subTest(inner=inner), self.assertRaises(ValueError):
                    browser_verdict(report)

    def test_failed_check_or_unbound_native_binary_cannot_start_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / 'native'
            native.mkdir()
            host, library = native / 'host', native / 'library'
            host.write_bytes(b'host')
            library.write_bytes(b'library')
            summary = root / 'summary.json'
            valid = {'decision': 'PASS', 'host_binary': str(host), 'native_library': str(library)}
            summary.write_text(json.dumps(valid), 'utf-8')
            self.assertEqual((host.resolve(), library.resolve()), checked_native(summary))
            outside = root / 'other-host'
            outside.write_bytes(b'host')
            for change in ({'decision': 'FAIL'}, {'host_binary': str(outside)},
                           {'native_library': str(native / 'missing')}):
                summary.write_text(json.dumps({**valid, **change}), 'utf-8')
                with self.subTest(change=change), self.assertRaises(ValueError):
                    checked_native(summary)
