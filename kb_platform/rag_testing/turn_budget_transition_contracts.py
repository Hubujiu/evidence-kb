import unittest
from unittest.mock import Mock, patch

from kb_platform.agent_model import AgentModel, BudgetLedger
from kb_platform.model_errors import ModelPreflightError, RemoteModelError
from .harness_context_model_contracts import _Provider


class TurnReservationTransitionContracts(unittest.TestCase):
    messages = [{'role': 'user', 'content': 'Synthetic reservation transition'}]

    def options(self, turn='turn', limit=3):
        return {'role': 'answer', 'schema': {'type': 'object'}, 'output_limit': 16,
                'call_context': {'session_id': 'session', 'turn_id': turn, 'max_physical_calls': limit}}

    def assert_quiescent_accounting(self, model, turn, *, calls, tokens, unknown):
        global_state = model.ledger.snapshot()
        turn_state = model.turn_budget_snapshot('session', turn)
        for state in (global_state, turn_state):
            self.assertEqual(state['calls'], calls)
            self.assertEqual(state['tokens'], tokens)
            self.assertEqual(state['unknown_usage_calls'], unknown)
            self.assertEqual(state['reserved_calls'], 0)
            self.assertEqual(state['reserved_tokens'], 0)
        self.assertEqual(global_state['accounted_tokens'], turn_state['accounted_tokens'])
        return global_state

    def test_global_call_refusal_releases_new_turn_reservation_without_provider_invocation(self):
        provider = _Provider([{}])
        model = AgentModel(provider, ledger=BudgetLedger(max_calls=1))
        model.complete(self.messages, **self.options(turn='first'))
        before = model.ledger.snapshot()
        with self.assertRaisesRegex(RemoteModelError, 'AGENT_CALL_BUDGET_EXHAUSTED') as caught:
            model.complete(self.messages, **self.options(turn='second'))
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(model.ledger.snapshot(), before)
        second = model.turn_budget_snapshot('session', 'second')
        for field in ('calls', 'tokens', 'accounted_tokens', 'unknown_usage_calls', 'reserved_calls', 'reserved_tokens'):
            self.assertEqual(second[field], 0)

    def test_global_token_refusal_releases_only_its_turn_reservation_not_another_global_owner(self):
        ledger = BudgetLedger(max_calls=10, max_tokens=2000)
        held = ledger.reserve(1, 2000)
        provider = _Provider([])
        model = AgentModel(provider, ledger=ledger)
        before = ledger.snapshot()
        with self.assertRaisesRegex(RemoteModelError, 'AGENT_TOKEN_BUDGET_EXHAUSTED') as caught:
            model.complete(self.messages, **self.options())
        self.assertIs(caught.exception.model_sent, False)
        self.assertEqual(provider.calls, [])
        self.assertEqual(ledger.snapshot(), before)
        turn = model.turn_budget_snapshot('session', 'turn')
        self.assertEqual(turn['calls'], 0)
        self.assertEqual(turn['reserved_calls'], 0)
        self.assertEqual(turn['reserved_tokens'], 0)
        ledger.settle(held, 0)
        self.assertEqual(ledger.snapshot()['reserved_calls'], 0)

    def test_observed_last_usage_and_unknown_prior_attempt_survive_validation_and_cache_errors(self):
        class RetryingProvider(_Provider):
            max_attempts = 2

            def complete(self, messages, output_limit, *, purpose, key, trace):
                trace['attempt'] += 1
                return super().complete(messages, output_limit, purpose=purpose, key=key, trace=trace)

        for mode in ('validation', 'cache', 'record'):
            with self.subTest(mode=mode):
                provider = RetryingProvider([{}])
                model = AgentModel(provider)
                options = self.options()
                cache_error = ModelPreflightError('SYNTHETIC_CACHE_FAILURE')
                record_error = RuntimeError('SYNTHETIC_RECORD_FAILURE')
                if mode == 'cache':
                    provider.cache_put = Mock(side_effect=cache_error)
                else:
                    options['validator'] = Mock(side_effect=ValueError('SYNTHETIC_VALIDATION_FAILURE'))
                    if mode == 'record':
                        provider.record = Mock(side_effect=record_error)
                expected = {'cache': ModelPreflightError, 'record': RuntimeError}.get(mode, RemoteModelError)
                with self.assertRaises(expected) as caught:
                    model.complete(self.messages, **options)
                if mode == 'cache':
                    self.assertIs(caught.exception, cache_error)
                if mode == 'record':
                    self.assertIs(caught.exception, record_error)
                self.assertIs(caught.exception.model_sent, True)
                self.assertIsNone(caught.exception.preflight_code)
                record = caught.exception.call_record
                self.assertEqual(record['attempts'], 2)
                self.assertEqual(record['usage']['total_tokens'], 15)
                self.assertTrue(record['usage']['available'])
                after = self.assert_quiescent_accounting(model, 'turn', calls=2, tokens=15, unknown=1)
                self.assertEqual(after['accounted_tokens'], 15 + record['estimated_input_tokens'] + 16)
                self.assertEqual(len(provider.calls), 1)

    def test_propagating_cancellation_keeps_both_unknown_charges_and_releases_reservations(self):
        class SyntheticCancellation(BaseException):
            pass

        signal = SyntheticCancellation('controlled provider cancellation')

        def cancel():
            raise signal

        provider = _Provider([{}], after=cancel)
        model = AgentModel(provider)
        with self.assertRaises(SyntheticCancellation) as caught:
            model.complete(self.messages, **self.options())
        self.assertIs(caught.exception, signal)
        after = self.assert_quiescent_accounting(model, 'turn', calls=1, tokens=0, unknown=1)
        self.assertGreater(after['accounted_tokens'], 0)
        self.assertEqual(len(provider.calls), 1)

    def test_application_cache_uses_zero_attempts_even_when_global_budget_is_full(self):
        ledger = BudgetLedger(max_calls=1)
        reservation = ledger.reserve(1, 20)
        ledger.settle(reservation, 1, {'available': True, 'total_tokens': 15})
        provider = _Provider([])
        provider.cache_get = Mock(return_value={'value': {}, 'finish_reason': 'stop',
                                              'usage': {'total_tokens': 15}})
        model = AgentModel(provider, ledger=ledger)
        before = ledger.snapshot()
        result = model.complete(self.messages, **self.options(turn='cached'))
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.usage['total_tokens'], 0)
        self.assertEqual(result.cache_kind, 'application_response')
        self.assertEqual(provider.calls, [])
        self.assertEqual(ledger.snapshot(), before)
        self.assertIsNone(model.turn_budget_snapshot('session', 'cached'))

    def test_global_then_turn_settlement_allows_transient_snapshot_difference(self):
        model = AgentModel(_Provider([{}]))
        settle = model.ledger.settle
        observed = []

        def settle_and_observe(*args, **kwargs):
            settle(*args, **kwargs)
            observed.append((model.ledger.snapshot(), model.turn_budget_snapshot('session', 'turn')))

        with patch.object(model.ledger, 'settle', side_effect=settle_and_observe):
            model.complete(self.messages, **self.options())
        self.assertEqual(len(observed), 1)
        global_state, turn_state = observed[0]
        self.assertEqual((global_state['calls'], global_state['reserved_calls']), (1, 0))
        self.assertEqual((turn_state['calls'], turn_state['reserved_calls']), (0, 1))
        self.assert_quiescent_accounting(model, 'turn', calls=1, tokens=15, unknown=0)

    def test_invalid_attempt_count_is_conservative_and_repeated_settlement_cannot_recharge(self):
        ledger = BudgetLedger(max_calls=2, max_tokens=20)
        reservation = ledger.reserve(2, 10)
        ledger.settle(reservation, 'invalid')
        before = ledger.snapshot()
        self.assertEqual(before['calls'], 2)
        self.assertEqual(before['unknown_usage_calls'], 2)
        self.assertEqual(before['accounted_tokens'], 20)
        self.assertEqual(before['reserved_calls'], 0)
        with self.assertRaises(KeyError):
            ledger.settle(reservation, 1, {'available': True, 'total_tokens': 1})
        self.assertEqual(ledger.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
