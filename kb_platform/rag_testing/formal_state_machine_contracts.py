from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from .formal_state_machines import configured_operators, parse_tlc, registry_violations, source_digest, tlc_verdict


def frame(code, severity, body):
    return f'@!@!@STARTMSG {code}:{severity} @!@!@\n{body}\n@!@!@ENDMSG {code} @!@!@\n'


def sample_log(*, violation=None, success=True):
    result = frame(2185, 0, 'Starting...')
    if violation:
        result += frame(2110, 1, f'Invariant {violation} is violated.')
        result += frame(2121, 1, 'The behavior up to this point is:')
    if success:
        result += frame(2193, 0, 'Model checking completed. No error has been found.')
    return result + frame(2199, 0, '3 states generated, 2 distinct states found, 0 states left on queue.') + frame(2186, 0, 'Finished')


class FormalRegistryContracts(unittest.TestCase):
    def fixture(self, root):
        (root / 'kb_platform').mkdir()
        (root / 'formal').mkdir()
        source = root / 'kb_platform/lifecycle.py'
        source.write_text('class Runtime:\n    def step(self, allowed):\n        if allowed:\n            self.state = "ready"\n', encoding='utf-8')
        binding = {'path': 'kb_platform/lifecycle.py', 'language': 'python', 'symbol': 'Runtime.step'}
        binding['sha256'] = source_digest(root, binding)
        return {'schema_version': 1, 'discovery': {'roots': ['kb_platform'], 'exclusions': []},
            'machines': [{'id': 'sample', 'status': 'unmodeled', 'owners': ['kb_platform/lifecycle.py'],
                'states': ['ready'], 'transitions': ['step'], 'invariants': ['Ready requires allowed'],
                'assumptions': ['Synthetic lint fixture'], 'exclusions': ['Not a model or product proof'],
                'source_bindings': [binding], 'models': [], 'negative_controls': []}]}

    def test_provider_circuit_constraint_survives_refreshed_source_hash(self):
        current = (Path(__file__).resolve().parents[1] / 'provider_runtime.py').read_text('utf-8')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            source = root / 'kb_platform/provider_runtime.py'
            source.write_text(current, encoding='utf-8')
            binding = {'path': 'kb_platform/provider_runtime.py', 'language': 'python', 'symbol': '<module>'}
            binding['sha256'] = source_digest(root, binding)
            registry['machines'][0]['source_bindings'].append(binding)
            self.assertEqual([], registry_violations(root, registry))
            source.write_text(current.replace('permit not in self._active or permit.generation != self._generation',
                                              'permit not in self._active', 1), encoding='utf-8')
            binding['sha256'] = source_digest(root, binding)
            violations = registry_violations(root, registry)
            self.assertTrue(any('provider-circuit:' in error for error in violations))
            self.assertFalse(any('source review drift' in error for error in violations))

    def test_reviewed_symbol_is_accepted_without_claiming_model_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            self.assertEqual([], registry_violations(root, registry))
            self.assertEqual('unmodeled', registry['machines'][0]['status'])

    def test_seeded_guard_and_state_mutations_are_rejected(self):
        for old, new in (('if allowed:', 'if not allowed:'), ('"ready"', '"destroyed"')):
            with self.subTest(mutation=new), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                source = root / 'kb_platform/lifecycle.py'
                source.write_text(source.read_text().replace(old, new), encoding='utf-8')
                self.assertTrue(any('source review drift' in error for error in registry_violations(root, registry)))

    def test_formatting_does_not_change_python_semantic_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            source = root / 'kb_platform/lifecycle.py'
            source.write_text(source.read_text().replace('if allowed:', 'if ( allowed ):'), encoding='utf-8')
            self.assertEqual([], registry_violations(root, registry))

    def test_new_transition_owner_requires_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            source = root / 'kb_platform/other.py'
            source.write_text('def hidden():\n    phase = "active"\n    return phase\n', encoding='utf-8')
            self.assertIn('Unregistered state candidate: kb_platform/other.py:hidden', registry_violations(root, registry))

    def test_discovery_exclusions_require_specific_reasons_and_cannot_go_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            registry['discovery']['exclusions'] = [{'path': 'kb_platform/lifecycle.py', 'symbol': 'missing', 'reason': 'HTTP status, not lifecycle'}]
            self.assertTrue(any('Stale discovery exclusion' in error for error in registry_violations(root, registry)))
            registry['discovery']['exclusions'][0]['reason'] = ''
            self.assertTrue(any('requires a unique' in error for error in registry_violations(root, registry)))

    def test_missing_source_and_escaping_source_are_rejected(self):
        for path in ('../outside.py', 'kb_platform/missing.py'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.fixture(root)
                registry['machines'][0]['source_bindings'][0]['path'] = path
                self.assertTrue(registry_violations(root, registry))

    def test_modeled_without_model_and_unregistered_model_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            changed = deepcopy(registry)
            changed['machines'][0]['status'] = 'partial'
            self.assertTrue(any('partial requires' in error for error in registry_violations(root, changed)))
            (root / 'formal/Hidden.tla').write_text('---- MODULE Hidden ----\n====\n', encoding='utf-8')
            self.assertTrue(any('Unregistered model input' in error for error in registry_violations(root, registry)))

    def test_cfg_invariants_and_temporal_properties_are_not_confused(self):
        text = 'SPECIFICATION Spec\nINVARIANTS TypeOK Safe\nPROPERTY EventuallyDone\nCONSTANT Count = 2\n'
        self.assertEqual({'TypeOK', 'Safe'}, configured_operators(text, {'INVARIANT', 'INVARIANTS'}))
        self.assertEqual({'EventuallyDone'}, configured_operators(text, {'PROPERTY', 'PROPERTIES'}))

    def test_negative_control_cannot_be_relabelled_as_a_positive_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            machine = registry['machines'][0]
            machine['negative_controls'] = [{'kind': 'positive'}]
            self.assertTrue(any('cannot change their expected failure kind' in error
                                for error in registry_violations(root, registry)))


class FormalVerdictContracts(unittest.TestCase):
    def test_positive_requires_complete_success_and_exhausted_reachable_states(self):
        log = sample_log()
        self.assertTrue(tlc_verdict(log, 0))
        self.assertEqual(2, parse_tlc(log)['states']['distinct'])
        for changed, code in ((log, 1), (sample_log(success=False), 0),
                              (log.replace('0 states left', '1 states left'), 0),
                              (log.replace('2 distinct states', '0 distinct states'), 0)):
            self.assertFalse(tlc_verdict(changed, code))

    def test_negative_requires_named_invariant_violation_not_any_error(self):
        log = sample_log(violation='NoPrematureRelease', success=False)
        self.assertTrue(tlc_verdict(log, 12, 'NoPrematureRelease'))
        self.assertFalse(tlc_verdict(log, 12, 'DifferentInvariant'))
        self.assertFalse(tlc_verdict(log, 151, 'NoPrematureRelease'))
        self.assertFalse(tlc_verdict(sample_log(success=False), 12, 'NoPrematureRelease'))

    def test_parse_failure_and_unframed_success_text_cannot_pass(self):
        parse_error = frame(2185, 0, 'Starting') + frame(2229, 1, 'Missing specification') + frame(2186, 0, 'Finished')
        self.assertFalse(tlc_verdict(parse_error, 151, 'NoPrematureRelease'))
        self.assertFalse(tlc_verdict('Model checking completed. No error has been found.', 0))

    def test_truncation_and_unrelated_error_frames_cannot_pass(self):
        log = sample_log()
        self.assertFalse(tlc_verdict(log + '@!@!@STARTMSG 2110:1 @!@!@\n', 0))
        self.assertFalse(tlc_verdict(log + frame(1000, 2, 'TLC bug'), 0))
        negative = sample_log(violation='NoPrematureRelease', success=False)
        self.assertFalse(tlc_verdict(negative + frame(2229, 1, 'Missing specification'), 12, 'NoPrematureRelease'))

    def test_liveness_requires_temporal_violation_not_shared_exit_code(self):
        base = sample_log(success=False) + frame(2192, 0, 'Checking temporal properties') + frame(2267, 0, 'Finished checking')
        self.assertFalse(tlc_verdict(base, 13, expected_property='EventuallyActive'))
        violation = base + frame(2116, 1, 'Temporal properties were violated.') + frame(2264, 1, 'Counter-example')
        self.assertTrue(tlc_verdict(violation, 13, expected_property='EventuallyActive'))
        self.assertFalse(tlc_verdict(violation + frame(2128, 1, 'Action property evaluation failed'), 13,
                                     expected_property='EventuallyActive'))


def live_suite(report):
    class ActualFormalEvidenceContracts(unittest.TestCase):
        def test_every_registered_run_executed_exactly_once(self):
            actual = {row['id']: row['kind'] for row in report['results']}
            self.assertEqual(report['expected_runs'], actual)
            self.assertEqual(len(actual), len(report['results']))
            self.assertIn('partial', report['coverage'].values())
            self.assertEqual([], report['violations'])

        def test_actual_positive_models_and_negative_controls_have_verified_outcomes(self):
            for row in report['results']:
                with self.subTest(model=row['id']):
                    self.assertEqual('PASS', row['decision'], json.dumps(row))
                    self.assertFalse(row['timeout'])
                    self.assertTrue(row['input_sha256'])
                    self.assertEqual({'positive': 0, 'negative': 12, 'liveness-negative': 13, 'invalid': 151}[row['kind']],
                                     row['returncode'])
    return unittest.defaultTestLoader.loadTestsFromTestCase(ActualFormalEvidenceContracts)
