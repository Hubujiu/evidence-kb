import unittest

from .harness_performance import _check_equal, _measure


class HarnessPerformanceContracts(unittest.TestCase):
    def test_failed_calls_remain_in_denominator_without_retries(self):
        calls = []

        def operation(index):
            calls.append(index)
            if index in (0, 3):
                raise RuntimeError('synthetic failure')
            return index

        report = _measure(operation, _check_equal, samples=5, warmup=2)
        self.assertEqual(calls, list(range(7)))
        self.assertEqual(report['samples'], 5)
        self.assertEqual(len(report['durations_ns']), 5)
        self.assertEqual(report['errors'], {'RuntimeError': 1})
        self.assertEqual(report['warmup_errors'], {'RuntimeError': 1})

    def test_wrong_results_are_reported_as_errors(self):
        report = _measure(lambda index: -1, _check_equal, samples=3, warmup=1)
        self.assertEqual(report['errors'], {'ValueError': 3})
        self.assertEqual(report['warmup_errors'], {'ValueError': 1})
        self.assertEqual(len(report['durations_ns']), 3)
