import unittest


def passed_contract_run(result: unittest.TestResult, planned: int) -> bool:
    return (result.wasSuccessful() and result.testsRun == planned > 0
            and not result.skipped and not result.expectedFailures)
