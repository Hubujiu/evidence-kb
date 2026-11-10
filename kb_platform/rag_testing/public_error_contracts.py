import unittest

from kb_platform.model_errors import RemoteModelError
from kb_platform.plugin_contracts import ContractError
from kb_platform.public_errors import public_error, public_error_headers
from kb_platform.knowledge_platform_contract import _call


class PublicErrorContracts(unittest.TestCase):
    def test_platform_model_error_conversion_keeps_response_and_recovery_facts(self):
        error = RemoteModelError('PROVIDER_LEDGER_ERROR', 200, model_sent=True)
        error.physical_request_id = 'physical-one'
        error.operation_id = 'settlement-op'
        error.observed_usage = {'input_tokens': 7, 'output_tokens': 3}

        def fail():
            raise error

        with self.assertRaises(ContractError) as caught:
            _call(fail)
        observed = public_error(caught.exception)
        self.assertEqual(observed['physical_request_id'], 'physical-one')
        self.assertEqual(observed['operation_id'], 'settlement-op')
        self.assertEqual(observed['observed_usage'], {'input_tokens': 7, 'output_tokens': 3})

    def test_unknown_commit_keeps_recovery_identity_and_discards_private_fields(self):
        error = ContractError('COMMIT_UNCONFIRMED', 'Awaiting reconciliation', status_code=503,
            details={'outcome': 'UNKNOWN', 'operation_id': 'op-123', 'model_sent': True,
                'physical_request_id': 'phys-123', 'payload': {'secret': 'hidden'},
                'path': '/private/ledger', 'token': 'private-token'})
        result = public_error(error)
        self.assertEqual(result['operation_id'], 'op-123')
        self.assertIs(result['model_sent'], True)
        self.assertEqual(result['physical_request_id'], 'phys-123')
        self.assertEqual(result['outcome'], 'UNKNOWN')
        self.assertEqual(result['recovery_action'], 'inspect_operation')
        self.assertFalse({'payload', 'path', 'token'} & result.keys())

    def test_provider_wait_and_usage_survive_wrapping_without_inventing_send_fact(self):
        provider = RemoteModelError('PROVIDER_HTTP_ERROR', 429, retry_after_seconds=15)
        provider.observed_usage = {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10,
            'private': 'hidden', 'reasoning_tokens': float('nan')}
        wrapped = ContractError(provider.code, str(provider), status_code=502)
        wrapped.__cause__ = provider
        result = public_error(wrapped)
        self.assertEqual(result['provider_http_status'], 429)
        self.assertEqual(result['retry_after_seconds'], 15)
        self.assertEqual(result['recovery_action'], 'wait')
        self.assertEqual(result['observed_usage'], {'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10})
        self.assertNotIn('model_sent', result)
        self.assertEqual(public_error_headers(result), {'Retry-After': '15'})
