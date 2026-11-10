from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
import unittest

from kb_platform.model_errors import RemoteModelError
from kb_platform.provider_runtime import ProviderCircuit, RuntimePolicyError
from kb_platform.remote import _provider_guard
from scripts.formal_provider_lint import provider_circuit_violations


class ProviderCircuitContracts(unittest.TestCase):
    def circuit(self, threshold=1):
        now = [0.0]
        return ProviderCircuit(threshold, 10.0, clock=lambda: now[0]), now

    def assert_blocked(self, circuit):
        with self.assertRaisesRegex(RuntimePolicyError, 'PROVIDER_CIRCUIT_OPEN'):
            circuit.acquire()

    def test_old_success_does_not_close_open_circuit_before_cooldown(self):
        circuit, _ = self.circuit()
        old, trip = circuit.acquire(), circuit.acquire()
        circuit.finish(trip, failure=True)
        expected = circuit.snapshot()
        circuit.finish(old, success=True)
        self.assertEqual(circuit.snapshot(), expected)
        self.assert_blocked(circuit)

    def test_old_failure_does_not_extend_open_cooldown(self):
        circuit, now = self.circuit()
        old, trip = circuit.acquire(), circuit.acquire()
        circuit.finish(trip, failure=True)
        expected = circuit.snapshot()
        now[0] = 9.0
        circuit.finish(old, failure=True)
        self.assertEqual(circuit.snapshot(), expected)
        now[0] = 10.0
        probe = circuit.acquire()
        self.assertEqual(circuit.snapshot()['state'], 'HALF_OPEN')
        circuit.finish(probe, success=True)

    def test_old_outcome_cannot_change_or_release_current_probe(self):
        for outcome in ('success', 'failure'):
            with self.subTest(outcome=outcome):
                circuit, now = self.circuit()
                old, trip = circuit.acquire(), circuit.acquire()
                circuit.finish(trip, failure=True)
                now[0] = 10.0
                probe = circuit.acquire()
                expected = circuit.snapshot()
                circuit.finish(old, **{outcome: True})
                self.assertEqual(circuit.snapshot(), expected)
                self.assert_blocked(circuit)
                circuit.finish(probe, success=True)
                self.assertEqual(circuit.snapshot()['state'], 'CLOSED')

    def test_old_outcome_cannot_change_new_closed_generation(self):
        for outcome in ('success', 'failure'):
            with self.subTest(outcome=outcome):
                circuit, now = self.circuit(threshold=2)
                old = circuit.acquire()
                circuit.finish(circuit.acquire(), failure=True)
                circuit.finish(circuit.acquire(), failure=True)
                now[0] = 10.0
                circuit.finish(circuit.acquire(), success=True)
                circuit.finish(circuit.acquire(), failure=True)
                expected = circuit.snapshot()
                self.assertEqual(expected['consecutive_failures'], 1)
                circuit.finish(old, **{outcome: True})
                self.assertEqual(circuit.snapshot(), expected)

    def test_normal_completion_is_consumed_once(self):
        circuit, _ = self.circuit(threshold=2)
        permit = circuit.acquire()
        circuit.finish(permit, failure=True)
        expected = circuit.snapshot()
        circuit.finish(permit, failure=True)
        circuit.finish(permit, success=True)
        self.assertEqual(circuit.snapshot(), expected)
        self.assertEqual(expected['state'], 'CLOSED')
        self.assertEqual(expected['consecutive_failures'], 1)

    def test_consumed_success_cannot_erase_later_failure(self):
        circuit, _ = self.circuit(threshold=2)
        permit = circuit.acquire()
        circuit.finish(permit, success=True)
        circuit.finish(circuit.acquire(), failure=True)
        circuit.finish(permit, success=True)
        self.assertEqual(circuit.snapshot()['consecutive_failures'], 1)

    def test_probe_failure_reopens_with_fresh_cooldown(self):
        circuit, now = self.circuit()
        circuit.finish(circuit.acquire(), failure=True)
        now[0] = 10.0
        probe = circuit.acquire()
        now[0] = 12.0
        circuit.finish(probe, failure=True)
        self.assertEqual(circuit.snapshot()['state'], 'OPEN')
        now[0] = 21.0
        self.assert_blocked(circuit)
        now[0] = 22.0
        circuit.finish(circuit.acquire(), success=True)

        self.assertEqual(circuit.snapshot()['state'], 'CLOSED')

    def test_neutral_probe_keeps_open_and_releases_only_its_slot(self):
        circuit, now = self.circuit()
        circuit.finish(circuit.acquire(), failure=True)
        now[0] = 10.0
        first = circuit.acquire()
        circuit.finish(first)
        self.assertEqual(circuit.snapshot()['state'], 'OPEN')
        self.assertEqual(circuit.snapshot()['consecutive_failures'], 1)
        second = circuit.acquire()
        circuit.finish(first, success=True)
        circuit.finish(first, failure=True)
        self.assertEqual(circuit.snapshot()['state'], 'HALF_OPEN')
        self.assert_blocked(circuit)
        circuit.finish(second, success=True)
        self.assertEqual(circuit.snapshot()['state'], 'CLOSED')

    def test_permits_are_owned_by_the_issuing_circuit(self):
        first, _ = self.circuit(threshold=2)
        second, _ = self.circuit(threshold=2)
        foreign, own = first.acquire(), second.acquire()
        second.finish(foreign, failure=True)
        self.assertEqual(second.snapshot()['consecutive_failures'], 0)
        second.finish(own, failure=True)
        self.assertEqual(second.snapshot()['consecutive_failures'], 1)
        first.finish(foreign, success=True)

    def test_concurrent_half_open_admission_allows_one_probe(self):
        circuit, now = self.circuit()
        circuit.finish(circuit.acquire(), failure=True)
        now[0] = 10.0
        barrier = Barrier(8)

        def acquire():
            barrier.wait(timeout=5)
            try:
                return circuit.acquire()
            except RuntimePolicyError as error:
                return error

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: acquire(), range(8)))
        permits = [value for value in results if not isinstance(value, RuntimePolicyError)]
        errors = [value for value in results if isinstance(value, RuntimePolicyError)]
        self.assertEqual(len(permits), 1)
        self.assertEqual([error.code for error in errors], ['PROVIDER_CIRCUIT_OPEN'] * 7)
        circuit.finish(permits[0], success=True)

    def test_provider_guard_preserves_old_result_without_admitting_during_probe(self):
        circuit, now = self.circuit()
        provider = SimpleNamespace(_circuit=circuit)
        entered = {'old': Event(), 'probe': Event()}
        release = {'old': Event(), 'probe': Event()}
        results, errors, calls = {}, {}, []

        @_provider_guard
        def operation(self, name):
            calls.append(name)
            if name == 'trip':
                raise RemoteModelError('PROVIDER_TRANSPORT_ERROR')
            if name in entered:
                entered[name].set()
                if not release[name].wait(timeout=5):
                    raise TimeoutError('synthetic fixture did not release')
            return name

        def run(name):
            try:
                results[name] = operation(provider, name)
            except BaseException as error:
                errors[name] = error

        threads = [Thread(target=run, args=(name,)) for name in ('old', 'probe')]
        threads[0].start()
        try:
            self.assertTrue(entered['old'].wait(timeout=5))
            with self.assertRaisesRegex(RemoteModelError, 'PROVIDER_TRANSPORT_ERROR'):
                operation(provider, 'trip')
            now[0] = 10.0
            threads[1].start()
            self.assertTrue(entered['probe'].wait(timeout=5))
            release['old'].set()
            threads[0].join(timeout=5)
            self.assertFalse(threads[0].is_alive())
            self.assertEqual(results, {'old': 'old'})
            with self.assertRaisesRegex(RemoteModelError, 'PROVIDER_CIRCUIT_OPEN'):
                operation(provider, 'must-not-run')
            self.assertEqual(calls, ['old', 'trip', 'probe'])
        finally:
            for event in release.values():
                event.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(errors, {})
        self.assertEqual(results, {'old': 'old', 'probe': 'probe'})
        self.assertEqual(circuit.snapshot()['state'], 'CLOSED')

    def test_admission_timeout_probe_is_neutral_and_original_error_survives(self):
        circuit, now = self.circuit()
        circuit.finish(circuit.acquire(), failure=True)
        now[0] = 10.0
        error = RemoteModelError('PROVIDER_DEADLINE_EXCEEDED')

        @_provider_guard
        def operation(self):
            raise error

        with self.assertRaises(RemoteModelError) as caught:
            operation(SimpleNamespace(_circuit=circuit))
        self.assertIs(caught.exception, error)
        self.assertEqual(circuit.snapshot()['state'], 'OPEN')
        self.assertEqual(circuit.snapshot()['consecutive_failures'], 1)
        circuit.finish(circuit.acquire(), success=True)


class ProviderCircuitLintContracts(unittest.TestCase):
    def source(self):
        return (Path(__file__).resolve().parents[1] / 'provider_runtime.py').read_text('utf-8')

    def test_current_source_satisfies_circuit_design_constraint(self):
        self.assertEqual(provider_circuit_violations(self.source()), [])

    def test_whitespace_and_unrelated_legal_edits_do_not_need_hash_exemptions(self):
        source = self.source().replace('with self._lock:', 'with (self._lock):')
        source = source.replace('@dataclass(frozen=True, eq=False)', '@dataclass(eq=False, frozen=True)')
        source += '\n\ndef unrelated_helper(value):\n    return value\n'
        self.assertEqual(provider_circuit_violations(source), [])

    def test_semantic_shape_mutations_are_rejected(self):
        mutations = [
            ('permit not in self._active or permit.generation != self._generation', 'permit not in self._active'),
            ('permit not in self._active or permit.generation != self._generation',
             'permit not in self._active and permit.generation != self._generation'),
            ('self._active.remove(permit)', 'pass'),
            ('self._active.remove(permit)', 'self._failures += 1\n            self._active.remove(permit)'),
            ('probing = permit is self._probe', 'probing = bool(self._probe)'),
            ('if probing:\n                self._probe = None', 'if True:\n                self._probe = None'),
            ('@dataclass(frozen=True, eq=False)', '@dataclass(frozen=True)'),
            ('def snapshot(self):\n        with self._lock:',
             'def snapshot(self):\n        self._opened_at = None\n        with self._lock:'),
            ('def snapshot(self):\n        with self._lock:',
             'def snapshot(self):\n        self._active.clear()\n        with self._lock:'),
            ('def snapshot(self):\n        with self._lock:',
             'def snapshot(self):\n        self._advance()\n        with self._lock:'),
            ('def finish(self, permit, *, failure=False, success=False):\n        with self._lock:',
             'def finish(self, permit, *, failure=False, success=False):\n        self._opened_at = None\n        with self._lock:'),
            ('def finish(self, permit, *, failure=False, success=False):\n        with self._lock:',
             'def finish(self, permit, *, failure=False, success=False):\n        with self._lock as permit:'),
        ]
        original = self.source()
        for old, new in mutations:
            with self.subTest(mutation=new):
                self.assertIn(old, original)
                self.assertTrue(provider_circuit_violations(original.replace(old, new, 1)))

    def test_missing_and_invalid_source_fail_closed(self):
        for source in ('', 'class ProviderCircuit:', 'class ProviderCircuit:\n    pass\n'):
            with self.subTest(source=source):
                self.assertTrue(provider_circuit_violations(source))
