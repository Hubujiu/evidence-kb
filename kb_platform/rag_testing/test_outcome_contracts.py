import io
import unittest

from .test_outcomes import passed_contract_run


class TestOutcomeContracts(unittest.TestCase):
    def run_case(self, method):
        case = type('SyntheticOutcome', (unittest.TestCase,), {'test_outcome': method})
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
        planned = suite.countTestCases()
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        return result, planned

    def test_only_complete_nonempty_success_passes(self):
        result, planned = self.run_case(lambda case: case.assertEqual(1, 1))
        self.assertTrue(passed_contract_run(result, planned))
        for count in (0, planned + 1, planned - 1):
            with self.subTest(planned=count):
                self.assertFalse(passed_contract_run(result, count))
        self.assertFalse(passed_contract_run(unittest.TestResult(), 0))

    def test_failure_error_skip_expected_failure_and_unexpected_success_fail(self):
        def error(case):
            raise ValueError('synthetic error')

        methods = (
            lambda case: case.fail('synthetic failure'),
            error,
            unittest.skip('synthetic skip')(lambda case: None),
            unittest.expectedFailure(lambda case: case.fail('synthetic expected failure')),
            unittest.expectedFailure(lambda case: None),
        )
        for method in methods:
            result, planned = self.run_case(method)
            with self.subTest(method=method):
                self.assertFalse(passed_contract_run(result, planned))
