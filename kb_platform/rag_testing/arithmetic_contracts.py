from unittest import TestCase

from kb_platform.agent_tools import bounded_calculate
from kb_platform.core import PlatformError


class ArithmeticBoundaryContracts(TestCase):
    def test_names_calls_attributes_and_containers_are_rejected(self):
        for expression in ('unknown', 'sum([1, 2])', '(1).__class__', '[1, 2]',
                           "__import__('math').sqrt(4)", 'True', '(lambda: 1)()'):
            with self.subTest(expression=expression):
                with self.assertRaises(PlatformError) as caught:
                    bounded_calculate(expression)
                self.assertEqual(caught.exception.code, 'INVALID_EXPRESSION')

    def test_resource_bounds_and_decimal_arithmetic(self):
        for expression in ('2 ** 33', '1 / 0', '1e101', '1e-101', '1+' * 300 + '1'):
            with self.subTest(expression=expression):
                with self.assertRaises(PlatformError) as caught:
                    bounded_calculate(expression)
                self.assertEqual(caught.exception.code, 'INVALID_EXPRESSION')
        self.assertEqual(str(bounded_calculate('0.1 + 0.2')), '0.3')
        self.assertEqual(str(bounded_calculate('-7 // 3')), '-3')
        self.assertEqual(str(bounded_calculate('-7 % 3')), '2')
