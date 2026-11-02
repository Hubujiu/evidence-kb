from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from . import formal_state_machines as runner
from .formal_inline_boolean import (
    CONTROL_EXPECTATIONS, FIXTURE_DIRECTORY, RULE, finding_signature,
    inline_boolean_findings, inline_control_fixtures,
)


ROOT = Path(__file__).resolve().parents[2]


def fixture_xml(identity):
    module = CONTROL_EXPECTATIONS[identity][0]
    return (ROOT / FIXTURE_DIRECTORY / identity / f'{module}.xml').read_bytes()


class OfficialInlineBooleanContracts(unittest.TestCase):
    def test_real_pr35_old_rhs_has_two_precise_review_findings(self):
        findings = inline_boolean_findings(fixture_xml('postflight-bad'), 'PostflightAuthority')
        self.assertEqual([(83, 'commitAllowed', 'evidenceAllowed'), (92, 'evidenceChecked', 'valid')],
                         finding_signature(findings))
        self.assertEqual({RULE}, {row['rule'] for row in findings})
        self.assertTrue(all(row['guard_column'] > row['assignment_end_column'] for row in findings))

    def test_real_pr44_old_rhs_guard_is_not_mistaken_for_another_primed_assignment(self):
        findings = inline_boolean_findings(fixture_xml('projection-bad'), 'ErrorProjection')
        self.assertEqual([(31, 'privateVisible', '=')], finding_signature(findings))

    def test_real_grouped_rhs_versions_have_no_inline_review_findings(self):
        for identity in ('postflight-good', 'projection-good'):
            with self.subTest(fixture=identity):
                module = CONTROL_EXPECTATIONS[identity][0]
                self.assertEqual([], inline_boolean_findings(fixture_xml(identity), module))

    def test_adjacent_assignments_unchanged_conditionals_and_grouped_rhs_are_excluded(self):
        self.assertEqual([], inline_boolean_findings(fixture_xml('safe-shapes'), 'InlineSafeShapes'))

    def test_intentional_inline_guard_still_requires_explicit_review_not_automatic_repair(self):
        rows = inline_boolean_findings(fixture_xml('intentional-guard'), 'InlineIntentionalGuard')
        self.assertEqual([(3, 'x', 'B')], finding_signature(rows))
        self.assertIn('otherwise put the intended guard on a separate conjunct line', rows[0]['message'])

    def test_declared_scope_does_not_claim_crossline_disjunction_or_chain_tail_detection(self):
        self.assertEqual([], inline_boolean_findings(fixture_xml('scope-limits'), 'InlineScopeLimits'))

    def test_official_fixtures_validate_without_invoking_java_or_subprocesses(self):
        with patch.object(subprocess, 'run', side_effect=AssertionError('Ordinary contracts cannot launch Java')) as process:
            fixtures = inline_control_fixtures(ROOT)
        process.assert_not_called()
        self.assertEqual(list(CONTROL_EXPECTATIONS), [row['id'] for row in fixtures])
        self.assertTrue(all(row['source_sha256'] and row['xml_sha256'] for row in fixtures))

    def test_invalid_or_truncated_xml_is_not_a_zero_finding_result(self):
        for xml in (b'', b'<modules>', b'<modules/>', fixture_xml('postflight-good')[:100]):
            with self.subTest(xml=xml[:20]), self.assertRaises(ValueError):
                inline_boolean_findings(xml, 'PostflightAuthority')

    def test_wrong_root_module_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'root module mismatch'):
            inline_boolean_findings(fixture_xml('postflight-good'), 'OtherModule')

    def test_missing_or_unknown_operator_uid_is_rejected(self):
        for mutation in ('missing', 'unknown'):
            with self.subTest(mutation=mutation):
                tree = ET.fromstring(fixture_xml('postflight-bad'))
                reference = tree.find('.//OpApplNode/operator')[0]
                if mutation == 'missing':
                    reference.remove(reference.find('UID'))
                else:
                    reference.find('UID').text = '999999'
                with self.assertRaises(ValueError):
                    inline_boolean_findings(ET.tostring(tree), 'PostflightAuthority')

    def test_duplicate_uid_and_missing_root_declaration_are_rejected(self):
        tree = ET.fromstring(fixture_xml('postflight-bad'))
        context = tree.find('context')
        context.append(deepcopy(context[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate context'):
            inline_boolean_findings(ET.tostring(tree), 'PostflightAuthority')
        tree = ET.fromstring(fixture_xml('postflight-bad'))
        for entry in list(tree.find('context')):
            if entry.findtext('ModuleNode/uniquename') == 'PostflightAuthority':
                tree.find('context').remove(entry)
        with self.assertRaisesRegex(ValueError, 'root module declaration'):
            inline_boolean_findings(ET.tostring(tree), 'PostflightAuthority')

    def test_missing_source_location_and_operands_are_errors_not_silent_skips(self):
        for element in ('location', 'operands'):
            with self.subTest(element=element):
                tree = ET.fromstring(fixture_xml('postflight-bad'))
                node = tree.find('.//OpApplNode')
                node.remove(node.find(element))
                with self.assertRaises(ValueError):
                    inline_boolean_findings(ET.tostring(tree), 'PostflightAuthority')

    def test_fixture_set_and_toolchain_cannot_be_silently_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / FIXTURE_DIRECTORY
            directory.mkdir(parents=True)
            manifest = json.loads((ROOT / FIXTURE_DIRECTORY / 'manifest.json').read_text('utf-8'))
            missing = deepcopy(manifest)
            del missing['fixtures']['projection-bad']
            (directory / 'manifest.json').write_text(json.dumps(missing), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'complete pinned fixture set'):
                inline_control_fixtures(root)
            (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (root / 'formal/toolchain-v1.json').write_text('{"sha256":"different"}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'pinned exporter toolchain'):
                inline_control_fixtures(root)

    def test_fixture_source_and_xml_digests_are_consumed(self):
        for name in ('PostflightAuthority.tla.txt', 'PostflightAuthority.xml'):
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(ROOT / FIXTURE_DIRECTORY, root / FIXTURE_DIRECTORY)
                shutil.copy2(ROOT / 'formal/toolchain-v1.json', root / 'formal/toolchain-v1.json')
                target = root / FIXTURE_DIRECTORY / 'postflight-bad' / name
                target.write_bytes(target.read_bytes() + b'changed')
                with self.assertRaisesRegex(ValueError, 'fixture integrity mismatch'):
                    inline_control_fixtures(root)


class InlineExportEvidenceContracts(unittest.TestCase):
    def export(self, directory, xml, *, code=0, expected=(), timeout=False):
        root = Path(directory)
        inputs = root / 'inputs'
        inputs.mkdir()

        def process(command, **kwargs):
            self.assertIn('tla2sany.xml.XMLExporter', command)
            self.assertIn('-o', command)
            self.assertEqual(30, kwargs['timeout'])
            if timeout:
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            kwargs['stdout'].write(xml)
            kwargs['stderr'].write(b'Synthetic subprocess result; not real exporter evidence\n')
            return SimpleNamespace(returncode=code)

        with patch.object(runner.subprocess, 'run', side_effect=process):
            row = runner.run_inline_export(root / 'not-executed.jar', root, inputs, 'example',
                'PostflightAuthority', {'formal/PostflightAuthority.tla': 'synthetic'}, list(expected))
        self.assertTrue((root / 'example/result.json').is_file())
        self.assertTrue((root / 'example/sany.log').is_file())
        return row

    def test_review_error_is_not_a_successful_positive_export(self):
        with tempfile.TemporaryDirectory() as directory:
            row = self.export(directory, fixture_xml('postflight-bad'))
        self.assertEqual('FAIL', row['decision'])
        self.assertEqual(0, row['returncode'])
        self.assertEqual(2, len(row['findings']))

    def test_negative_control_requires_exact_named_locations_not_any_diagnostic(self):
        expected = [(83, 'commitAllowed', 'evidenceAllowed'), (92, 'evidenceChecked', 'valid')]
        with tempfile.TemporaryDirectory() as directory:
            row = self.export(directory, fixture_xml('postflight-bad'), expected=expected)
        self.assertEqual('PASS', row['decision'])
        with tempfile.TemporaryDirectory() as directory:
            row = self.export(directory, fixture_xml('postflight-bad'), expected=[(83, 'wrong', 'valid')])
        self.assertEqual('FAIL', row['decision'])

    def test_parse_failure_nonzero_exit_and_timeout_cannot_satisfy_negative_control(self):
        for xml, code, timeout in ((b'', 0, False), (fixture_xml('postflight-bad'), 1, False),
                                   (b'', 0, True)):
            with self.subTest(code=code, timeout=timeout), tempfile.TemporaryDirectory() as directory:
                row = self.export(directory, xml, code=code, timeout=timeout,
                    expected=[(83, 'commitAllowed', 'evidenceAllowed'), (92, 'evidenceChecked', 'valid')])
                self.assertEqual('ERROR', row['decision'])
                self.assertIsNone(row['findings'])
                self.assertEqual(timeout, row['timeout'])

    def test_required_formal_run_stops_before_tlc_if_inline_review_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'formal').mkdir()
            registry = {'machines': [{'id': 'sample', 'status': 'partial', 'models': [], 'negative_controls': []}]}
            (root / runner.REGISTRY).write_text(json.dumps(registry), encoding='utf-8')
            jar = root / 'fixture.jar'
            jar.write_bytes(b'synthetic jar; never executed')
            lock = {'sha256': runner.digest(jar.read_bytes()), 'size_bytes': jar.stat().st_size,
                    'java_major': 21, 'default_jar': 'fixture.jar'}
            (root / runner.LOCK).write_text(json.dumps(lock), encoding='utf-8')
            version = SimpleNamespace(returncode=0, stdout='', stderr='openjdk version "21.0.1"')
            with patch.object(runner, 'registry_violations', return_value=[]), \
                    patch.object(runner.subprocess, 'run', return_value=version), \
                    patch.object(runner, 'run_inline_review', return_value={'decision': 'FAIL'}), \
                    patch.object(runner, 'run_model', side_effect=AssertionError('TLC must not run')) as tlc:
                result = runner.run_formal(root, root / 'output', jar)
            tlc.assert_not_called()
            self.assertEqual(1, result)
            report = json.loads((root / 'output/summary.json').read_text('utf-8'))
            self.assertEqual('FAIL', report['decision'])
            self.assertEqual([], report['results'])
            self.assertEqual('FAIL', report['inline_boolean_review']['decision'])
