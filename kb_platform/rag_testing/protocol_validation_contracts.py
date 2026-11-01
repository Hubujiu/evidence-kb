from contextlib import contextmanager
from functools import partial
from typing import Protocol
import unittest

from kb_platform.protocol_validation import callable_accepts, valid_callable


class CallProtocol(Protocol):
    def query(self, index, query, limit=10, *, filters=None): ...


class ProtocolValidationContracts(unittest.TestCase):
    def test_call_shape_is_checked_without_executing_callback(self):
        calls = []

        def callback(value, *, option=None):
            calls.append(value)

        self.assertTrue(valid_callable(callback, 'input', option='value'))
        self.assertFalse(valid_callable(callback))
        self.assertFalse(valid_callable(callback, 'input', 'unexpected'))
        self.assertFalse(valid_callable(callback, 'input', missing='value'))
        self.assertEqual(calls, [])

    def test_async_and_generator_functions_and_callable_objects_are_rejected(self):
        async def asynchronous(value):
            return value

        def generator(value):
            yield value

        async def async_generator(value):
            yield value

        for callback in (asynchronous, generator, async_generator):
            with self.subTest(callback=callback):
                self.assertFalse(valid_callable(callback, None))
                self.assertFalse(valid_callable(partial(callback, None)))
                instance = type('CallableFixture', (), {'__call__': staticmethod(callback)})()
                self.assertFalse(valid_callable(instance, None))

    def test_protocol_parameters_accept_keyword_and_positional_calls(self):
        def correct(index, query, limit=10, *, filters=None):
            return []

        def positional_only(index, query, limit=10, /, *, filters=None):
            return []

        def required_option(index, query, limit, *, filters=None):
            return []

        def missing_option(index, query, limit=10):
            return []

        def renamed_parameter(collection, query, limit=10, *, filters=None):
            return []

        self.assertTrue(callable_accepts(correct, CallProtocol.query))
        self.assertTrue(callable_accepts(lambda *args, **kwargs: [], CallProtocol.query))
        for callback in (positional_only, required_option, missing_option, renamed_parameter):
            with self.subTest(callback=callback):
                self.assertFalse(callable_accepts(callback, CallProtocol.query))

    def test_protocol_inherited_stub_is_rejected(self):
        class Incomplete(CallProtocol):
            pass

        self.assertFalse(callable_accepts(Incomplete().query, CallProtocol.query))

    def test_contextmanager_wrapper_is_a_valid_synchronous_factory(self):
        @contextmanager
        def transaction(*, write=True):
            yield write

        self.assertTrue(valid_callable(transaction, write=False))

    def test_positional_only_and_required_keyword_protocol_parameters(self):
        class PositionalProtocol(Protocol):
            def method(self, value, /, *, required, optional=None): ...

        def implementation(value, /, *, required, optional=None):
            return required

        self.assertTrue(callable_accepts(implementation, PositionalProtocol.method))
