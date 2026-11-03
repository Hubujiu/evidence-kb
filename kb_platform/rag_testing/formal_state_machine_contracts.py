from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .formal_state_machines import (configured_operators, digest, parse_tlc, registry_violations,
                                    run_model, source_digest, tlc_verdict)


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
    def test_lifecycle_booleans_and_executable_scripts_are_discovered(self):
        from .formal_state_machines import state_candidates

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'scripts/service.py').write_text(
                'class Service:\n    def close(self):\n        self._closed = True\n'
                '    def enter(self):\n        self._active += 1\n', encoding='utf-8')
            self.assertEqual(state_candidates(root, ['scripts']), {
                ('scripts/service.py', 'Service.close'), ('scripts/service.py', 'Service.enter')})

    def test_schema_two_invokes_composition_gate_without_changing_schema_one_fixtures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            with patch('kb_platform.rag_testing.formal_composition.composition_violations',
                       return_value=['composition: rejected fixture']) as check:
                self.assertEqual(registry_violations(root, registry), [])
                check.assert_not_called()
                registry['schema_version'] = 2
                self.assertIn('composition: rejected fixture', registry_violations(root, registry))
                check.assert_called_once_with(root, registry)

    def test_production_registry_cannot_downgrade_to_fixture_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            (root / 'formal/registry-v1.json').write_text(json.dumps(registry), encoding='utf-8')
            self.assertIn('Production registry requires schema v2 composition validation',
                          registry_violations(root))

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
    def test_physical_settlement_constraint_survives_refreshed_source_hash(self):
        repository = Path(__file__).resolve().parents[2]
        from scripts.physical_settlement_lint import CAPTURE_CONSUMERS

        owners = tuple(dict.fromkeys(['kb_platform/rag_testing/provider_capture.py',
            'kb_platform/google_semantic_provider.py', *[row[0] for row in CAPTURE_CONSUMERS]]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.fixture(root)
            bindings = {}
            for name in owners:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((repository / name).read_text('utf-8'), encoding='utf-8')
                binding = {'path': name, 'language': 'python', 'symbol': '<module>'}
                binding['sha256'] = source_digest(root, binding)
                registry['machines'][0]['source_bindings'].append(binding)
                bindings[name] = binding
            self.assertEqual([], registry_violations(root, registry))
            target = root / owners[0]
            original = target.read_text('utf-8')
            mutants = (("if 'ledger_settlement' in record:", 'if False:'),
                       ("if 'http_status' in record:", 'if True:'),
                       ("body.get('usage') if model and isinstance(body, dict) else None", "record.get('usage')"),
                       ('                preserve_observed(error)\n', ''),
                       ('                    preserve_observed(capture_error)\n', ''),
                       ('            record.update(http_status=response.status_code, response=body)\n'
                        "            settle(state, actual, None if state == 'COMPLETE' else f'HTTP_{response.status_code}')",
                        "            settle(state, actual, None if state == 'COMPLETE' else f'HTTP_{response.status_code}')\n"
                        '            record.update(http_status=response.status_code, response=body)'),
                       ('\n        except BaseException as exc:\n            exc.model_sent = False\n',
                        '\n        except BaseException as exc:\n            exc.model_sent = True\n'))
            for before, after in mutants:
                with self.subTest(mutation=before):
                    self.assertIn(before, original)
                    target.write_text(original.replace(before, after, 1), encoding='utf-8')
                    bindings[owners[0]]['sha256'] = source_digest(root, bindings[owners[0]])
                    errors = registry_violations(root, registry)
                    self.assertTrue(any('physical settlement:' in error for error in errors))
                    self.assertFalse(any('source review drift' in error for error in errors))

    def modeled_fixture(self, root):
        registry = self.fixture(root)
        (root / 'formal/Model.tla').write_text('---- MODULE Model ----\nVARIABLE state\nInit == state = 0\n'
            'Next == UNCHANGED state\nSpec == Init /\\ [][Next]_state\nTypeOK == state = 0\n====\n', encoding='utf-8')
        (root / 'formal/Model.cfg').write_text('SPECIFICATION Spec\nINVARIANT TypeOK\n', encoding='utf-8')
        (root / 'formal/ModelBug.cfg').write_text('SPECIFICATION Spec\nINVARIANT TypeOK\n', encoding='utf-8')
        machine = registry['machines'][0]
        machine.update(status='partial', remaining_obligations=['Synthetic lint fixture, not a proof'])
        machine['models'] = [{'id': 'sample-model', 'module': 'formal/Model.tla', 'config': 'formal/Model.cfg',
            'invariants': ['TypeOK'], 'properties': [], 'bounds': {'states': 2}, 'timeout_seconds': 30, 'heap_mb': 128}]
        machine['negative_controls'] = [{'id': 'sample-bug', 'module': 'formal/Model.tla', 'config': 'formal/ModelBug.cfg',
            'expected_invariant': 'TypeOK', 'timeout_seconds': 30, 'heap_mb': 128}]
        return registry

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

    def test_python_fingerprint_is_identical_across_supported_ast_dump_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'portable.py'
            source.write_text('def consume(flag):\n    if flag:\n        return 1\n    return 0\n', encoding='utf-8')
            expected = ("FunctionDef(name='consume', args=arguments(posonlyargs=[], args=[arg(arg='flag')], "
                        "kwonlyargs=[], kw_defaults=[], defaults=[]), body=[If(test=Name(id='flag', ctx=Load()), "
                        "body=[Return(value=Constant(value=1))], orelse=[]), Return(value=Constant(value=0))], "
                        "decorator_list=[], type_params=[])")
            binding = {'path': 'portable.py', 'language': 'python', 'symbol': 'consume'}
            self.assertEqual(digest(expected.encode()), source_digest(root, binding))

    def test_rust_fingerprint_normalizes_checkout_crlf_but_retains_source_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'portable.rs'
            text = b'pub fn step(allowed: bool) -> bool {\n    allowed\n}\n'
            binding = {'path': 'portable.rs', 'language': 'rust', 'symbol': 'step'}
            source.write_bytes(text)
            expected = source_digest(root, binding)
            self.assertEqual(digest(text), expected)
            source.write_bytes(text.replace(b'\n', b'\r\n'))
            self.assertEqual(expected, source_digest(root, binding))
            source.write_bytes(text.replace(b'    allowed', b'    !allowed'))
            self.assertNotEqual(expected, source_digest(root, binding))
            source.write_bytes(text.replace(b'\n', b'\r'))
            self.assertNotEqual(expected, source_digest(root, binding))

    def test_positive_model_cannot_drop_every_check_and_refresh_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self.modeled_fixture(root)
            self.assertEqual([], registry_violations(root, registry))
            registry['machines'][0]['models'][0]['invariants'] = []
            (root / 'formal/Model.cfg').write_text('SPECIFICATION Spec\n', encoding='utf-8')
            self.assertTrue(any('positive models require a nonempty' in error
                                for error in registry_violations(root, registry)))

    def test_model_operator_lists_bounds_and_limits_require_explicit_types(self):
        mutations = [('invariants', 'TypeOK'), ('invariants', ['TypeOK', 'TypeOK']), ('invariants', [1]),
                     ('properties', ''), ('properties', [None]), ('bounds', [2]), ('timeout_seconds', True)]
        for key, value in mutations:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                registry = self.modeled_fixture(root)
                registry['machines'][0]['models'][0][key] = value
                self.assertTrue(registry_violations(root, registry))

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


class FormalLimitationContracts(unittest.TestCase):
    def fixture(self, root):
        registry = FormalRegistryContracts().modeled_fixture(root)
        (root / 'formal/ModelLimit.cfg').write_text('SPECIFICATION Spec\nINVARIANT KnownLimit\n', encoding='utf-8')
        limitation = {'id': 'sample-limitation', 'kind': 'limitation', 'model_id': 'sample-model',
            'module': 'formal/Model.tla', 'config': 'formal/ModelLimit.cfg', 'expected_invariant': 'KnownLimit',
            'purpose': 'Synthetic stronger property outside the declared positive model contract',
            'timeout_seconds': 30, 'heap_mb': 128}
        registry['machines'][0]['known_limitations'] = [limitation]
        return registry, limitation

    def test_limitation_requires_association_explanation_and_separate_named_invariant_config(self):
        mutations = [('kind', 'negative'), ('model_id', 'missing-model'), ('module', 'formal/Other.tla'),
                     ('purpose', ''), ('purpose', None), ('config', 'formal/Model.cfg'),
                     ('expected_invariant', 'OtherLimit'), ('expected_property', 'EventuallySafe')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, limitation = self.fixture(root)
            self.assertEqual([], registry_violations(root, registry))
            for key, value in mutations:
                with self.subTest(key=key, value=value):
                    changed = deepcopy(registry)
                    changed['machines'][0]['known_limitations'][0][key] = value
                    self.assertTrue(registry_violations(root, changed))
            for extra in ('\nINVARIANT AnotherInvariant\n', '\nPROPERTY EventuallySafe\n'):
                (root / limitation['config']).write_text('SPECIFICATION Spec\nINVARIANT KnownLimit\n' + extra,
                                                         encoding='utf-8')
                self.assertTrue(any('must isolate its named safety invariant' in error
                                    for error in registry_violations(root, registry)))

    def test_limitation_cannot_replace_negative_control_or_register_an_unmodeled_family(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, limitation = self.fixture(root)
            registry['machines'][0]['negative_controls'] = []
            self.assertTrue(any('require a seeded negative control' in error
                                for error in registry_violations(root, registry)))
            registry['machines'][0]['negative_controls'] = [limitation]
            self.assertTrue(any('cannot change their expected failure kind' in error
                                for error in registry_violations(root, registry)))
            registry['machines'][0].update(status='unmodeled', models=[], negative_controls=[])
            self.assertTrue(any('must name a positive model' in error
                                for error in registry_violations(root, registry)))

    def test_limitation_configuration_remains_subject_to_registration_and_positive_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, _ = self.fixture(root)
            changed = deepcopy(registry)
            changed['machines'][0]['known_limitations'] = []
            self.assertIn('Unregistered model input: formal/ModelLimit.cfg', registry_violations(root, changed))
            registry['machines'][0]['models'][0]['invariants'] = []
            (root / 'formal/Model.cfg').write_text('SPECIFICATION Spec\n', encoding='utf-8')
            self.assertTrue(any('positive models require a nonempty' in error
                                for error in registry_violations(root, registry)))

    def test_limitation_cannot_mutate_the_associated_positive_model_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry, limitation = self.fixture(root)
            path = root / limitation['config']
            original = path.read_text('utf-8')
            for changed in (original + '\nCONSTANT Unsafe = TRUE\n', original + '\nCONSTRAINT HideBug\n',
                            original.replace('SPECIFICATION Spec', 'SPECIFICATION OtherSpec')):
                with self.subTest(configuration=changed):
                    path.write_text(changed, encoding='utf-8')
                    self.assertTrue(any('must preserve its positive model configuration behavior' in error
                                        for error in registry_violations(root, registry)))

    def test_run_model_reports_only_actual_named_limitation_as_observed_never_pass(self):
        cases = [(sample_log(violation='KnownLimit', success=False), 12, 'OBSERVED_LIMITATION'),
                 (sample_log(violation='OtherLimit', success=False), 12, 'FAIL'),
                 (sample_log(), 0, 'FAIL'), (sample_log(success=False), 151, 'FAIL'),
                 (sample_log(violation='KnownLimit', success=False), 151, 'FAIL')]
        for log, code, decision in cases:
            with self.subTest(code=code, decision=decision), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, spec = self.fixture(root)
                output = root / 'output'
                output.mkdir()

                def invoke(*args, **kwargs):
                    kwargs['stdout'].write(log)
                    return SimpleNamespace(returncode=code)

                with patch('kb_platform.rag_testing.formal_state_machines.subprocess.run', side_effect=invoke):
                    result = run_model(root, root / 'unused.jar', output, spec)
                self.assertEqual(decision, result['decision'])
                self.assertEqual('limitation', result['kind'])
                self.assertEqual(spec['purpose'], result['purpose'])
                self.assertEqual(spec['model_id'], result['model_id'])
                self.assertEqual(result, json.loads((output / spec['id'] / 'result.json').read_text('utf-8')))

    def test_timeout_is_not_an_observed_limitation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, spec = self.fixture(root)
            output = root / 'output'
            output.mkdir()
            with patch('kb_platform.rag_testing.formal_state_machines.subprocess.run',
                       side_effect=subprocess.TimeoutExpired('java', 30)):
                result = run_model(root, root / 'unused.jar', output, spec)
            self.assertEqual('FAIL', result['decision'])
            self.assertTrue(result['timeout'])

    def test_evidence_keeps_limitations_visible_and_ordinary_runs_required(self):
        ordinary = [{'id': 'sample-model', 'kind': 'positive', 'decision': 'PASS', 'returncode': 0},
                    {'id': 'sample-bug', 'kind': 'negative', 'decision': 'PASS', 'returncode': 12}]
        metadata = {'model_id': 'sample-model', 'expected_invariant': 'KnownLimit', 'purpose': 'Explicit limitation'}
        observation = {'id': 'sample-limitation', 'kind': 'limitation', 'decision': 'OBSERVED_LIMITATION',
                       'returncode': 12, **metadata}
        for row in ordinary + [observation]:
            row.update(timeout=False, input_sha256={'formal/Model.tla': 'synthetic-input-hash'})
        report = {'results': ordinary, 'expected_runs': {row['id']: row['kind'] for row in ordinary},
                  'coverage': {'sample': 'partial'}, 'violations': [], 'known_limitations': [observation],
                  'expected_limitations': {'sample-limitation': metadata}}
        from .formal_inline_boolean import CONTROL_EXPECTATIONS

        def synthetic_export(identity, expected):
            return {'id': identity, 'decision': 'PASS', 'returncode': 0, 'timeout': False,
                    'input_sha256': {'formal/Model.tla': 'synthetic-not-executed'},
                    'xml_sha256': 'synthetic-not-executed',
                    'expected_findings': [list(row) for row in expected],
                    'findings': [{'line': line, 'assignment': assignment, 'guard_operator': guard}
                                 for line, assignment, guard in expected]}

        report['inline_boolean_review'] = {
            'decision': 'PASS', 'expected_modules': ['formal/Model.tla'],
            'expected_controls': list(CONTROL_EXPECTATIONS),
            'modules': [{**synthetic_export('model-fixture', []), 'source_path': 'formal/Model.tla'}],
            'controls': [{**synthetic_export(identity, expected), 'fixture_id': identity}
                         for identity, (_, expected) in CONTROL_EXPECTATIONS.items()]}
        result = unittest.TestResult()
        live_suite(report).run(result)
        self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))
        for mutation in ('hide', 'relabel', 'replace-negative', 'mix-positive', 'unexpected-success', 'unnamed', 'inline-review'):
            changed = deepcopy(report)
            if mutation == 'hide':
                changed['known_limitations'] = []
            elif mutation == 'relabel':
                changed['known_limitations'][0]['decision'] = 'PASS'
            elif mutation == 'replace-negative':
                changed['results'].pop()
            elif mutation == 'mix-positive':
                changed['results'].append(changed['known_limitations'].pop())
            elif mutation == 'unexpected-success':
                changed['known_limitations'][0]['returncode'] = 0
            elif mutation == 'inline-review':
                changed['inline_boolean_review']['decision'] = 'FAIL'
            else:
                changed['known_limitations'][0]['expected_invariant'] = 'OtherInvariant'
            with self.subTest(mutation=mutation):
                result = unittest.TestResult()
                live_suite(changed).run(result)
                self.assertFalse(result.wasSuccessful())


def live_suite(report):
    class ActualFormalEvidenceContracts(unittest.TestCase):
        def test_official_inline_review_and_named_controls_ran_before_model_claims(self):
            from .formal_inline_boolean import CONTROL_EXPECTATIONS, finding_signature
            review = report['inline_boolean_review']
            self.assertEqual('PASS', review['decision'])
            self.assertEqual(review['expected_modules'], [row['source_path'] for row in review['modules']])
            self.assertEqual(set(CONTROL_EXPECTATIONS), set(review['expected_controls']))
            self.assertEqual(list(CONTROL_EXPECTATIONS), [row['fixture_id'] for row in review['controls']])
            self.assertTrue(review['modules'])
            for row in review['modules'] + review['controls']:
                with self.subTest(export=row['id']):
                    self.assertEqual('PASS', row['decision'])
                    self.assertEqual(0, row['returncode'])
                    self.assertFalse(row['timeout'])
                    self.assertTrue(row['input_sha256'])
                    self.assertTrue(row['xml_sha256'])
                    self.assertEqual(row['expected_findings'], [list(item) for item in finding_signature(row['findings'])])

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

        def test_known_limitations_are_separate_observations_not_model_passes(self):
            actual = {row['id']: {key: row[key] for key in ('model_id', 'expected_invariant', 'purpose')}
                      for row in report['known_limitations']}
            self.assertEqual(report['expected_limitations'], actual)
            self.assertEqual(len(actual), len(report['known_limitations']))
            self.assertFalse(set(actual).intersection(report['expected_runs']))
            positive_models = {row['id'] for row in report['results'] if row['kind'] == 'positive'}
            for row in report['known_limitations']:
                with self.subTest(limitation=row['id']):
                    self.assertEqual('limitation', row['kind'])
                    self.assertEqual('OBSERVED_LIMITATION', row['decision'], json.dumps(row))
                    self.assertEqual(12, row['returncode'])
                    self.assertFalse(row['timeout'])
                    self.assertTrue(row['input_sha256'])
                    self.assertIn(row['model_id'], positive_models)
                    self.assertTrue(row['purpose'].strip())
    return unittest.defaultTestLoader.loadTestsFromTestCase(ActualFormalEvidenceContracts)
