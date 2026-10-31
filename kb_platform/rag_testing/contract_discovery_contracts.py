from copy import deepcopy
import unittest

from scripts.check_project import combine_contract_reports


class ContractDiscoveryContracts(unittest.TestCase):
    def reports(self):
        discovered = {'one.a': 'one', 'one.b': 'one', 'two.a': 'two', 'two.b': 'two'}
        return [dict(decision='PASS', discovered=discovered, selected=selected,
                     total=2, failures=0, errors=0, skipped=0, expected_failures=0)
                for selected in (['one.a', 'two.a'], ['one.b', 'two.b'])]

    def test_complete_discovery_preserves_all_counts(self):
        report = combine_contract_reports(self.reports(), ['one', 'two'])
        self.assertEqual(report['decision'], 'PASS')
        self.assertEqual(report['total'], 4)

    def test_missing_duplicate_empty_or_incomplete_shard_cannot_pass(self):
        original = self.reports()
        variants = [original[:1], [original[0], original[0]],
                    [original[0], {**original[1], 'selected': [], 'total': 0}],
                    [original[0], {**original[1], 'total': 1}],
                    [original[0], {**original[1], 'discovered': {'one.a': 'one'}}],
                    [original[0], {'decision': 'FAIL', 'discovered': {}, 'total': 0}]]
        for reports in variants:
            with self.subTest(reports=reports):
                self.assertEqual(combine_contract_reports(reports, ['one', 'two'])['decision'], 'FAIL')

    def test_failure_error_skip_expected_failure_and_exit_status_remain_failures(self):
        for key in ('failures', 'errors', 'skipped', 'expected_failures', 'decision'):
            reports = deepcopy(self.reports())
            reports[1][key] = 'FAIL' if key == 'decision' else 1
            with self.subTest(key=key):
                self.assertEqual(combine_contract_reports(reports, ['one', 'two'])['decision'], 'FAIL')
