from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from kb_platform import host_applications
from kb_platform.host_applications import HttpApplication


class HttpLifespanOwnerContracts(unittest.TestCase):
    def application(self, fail=None, started=True):
        events = []
        @asynccontextmanager
        async def lifespan(app):
            events.append('enter-attempt')
            if fail == 'enter':
                raise ValueError('enter')
            events.append('entered')
            try:
                yield
            finally:
                events.append('exit-attempt')
                if fail == 'exit':
                    raise ValueError('exit')
        async def shutdown():
            events.append('fallback-attempt')
            if fail == 'fallback':
                raise ValueError('fallback')
        async def serve():
            events.append('serve')
            if fail == 'serve':
                raise ValueError('serve')
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan, _shutdown=shutdown))
        server = SimpleNamespace(serve=serve, started=started, should_exit=False,
                                 config=SimpleNamespace(port=0))
        with patch('kb_platform.host_applications.uvicorn.Config'), \
                patch('kb_platform.host_applications.uvicorn.Server', return_value=server):
            application = HttpApplication(app, {})
        return application, events

    def test_success_and_server_failure_exit_once_without_fallback(self):
        for failure in (None, 'serve', 'exit'):
            with self.subTest(failure=failure):
                app, events = self.application(failure)
                if failure is None:
                    app.run()
                else:
                    with self.assertRaisesRegex(ValueError, failure):
                        app.run()
                app.close()
                app.close()
                self.assertEqual(events.count('exit-attempt'), 1)
                self.assertNotIn('fallback-attempt', events)
                self.assertTrue(app._finished)

    def test_entry_failure_falls_back_once_but_does_not_claim_context_exit(self):
        app, events = self.application('enter')
        with self.assertRaisesRegex(ValueError, 'enter'):
            app.run()
        self.assertFalse(app._entered)
        self.assertFalse(app._finished)
        app.close()
        app.close()
        self.assertEqual(events, ['enter-attempt', 'fallback-attempt'])

    def test_never_run_close_and_stop_do_not_start_lifespan(self):
        app, events = self.application()
        app.stop()
        self.assertTrue(app.server.should_exit)
        app.close()
        app.close()
        self.assertEqual(events, ['fallback-attempt'])

    def test_fallback_failure_is_not_marked_finished_and_remains_explicitly_retryable(self):
        app, events = self.application('fallback')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'fallback'):
                app.close()
            self.assertFalse(app._finished)
        self.assertEqual(events, ['fallback-attempt', 'fallback-attempt'])

    def test_server_not_started_propagates_after_lifespan_exit(self):
        app, events = self.application(started=False)
        with self.assertRaisesRegex(RuntimeError, 'HOST_HTTP_START_FAILED'):
            app.run()
        app.close()
        self.assertEqual(events, ['enter-attempt', 'entered', 'serve', 'exit-attempt'])


class LockedFastAPILifespanIntegration(unittest.TestCase):
    def real_app(self, *, fail_enter=False):
        events = []
        app = FastAPI()
        def startup():
            events.append('startup')
            if fail_enter:
                raise RuntimeError('controlled startup failure')
        def sync_shutdown():
            events.append('sync-shutdown')
        async def async_shutdown():
            events.append('async-shutdown')
        app.router.add_event_handler('startup', startup)
        app.router.add_event_handler('shutdown', sync_shutdown)
        app.router.add_event_handler('shutdown', async_shutdown)
        return app, events

    def test_locked_framework_never_run_close_uses_its_dispatcher_without_startup(self):
        app, events = self.real_app()
        application = HttpApplication(app, {'port': 0})
        application.close()
        application.close()
        self.assertEqual(events, ['sync-shutdown', 'async-shutdown'])

    def test_locked_framework_enter_failure_still_releases_created_resources(self):
        app, events = self.real_app(fail_enter=True)
        application = HttpApplication(app, {'port': 0})
        with self.assertRaisesRegex(RuntimeError, 'controlled startup failure'):
            application.run()
        application.close()
        self.assertEqual(events, ['startup', 'sync-shutdown', 'async-shutdown'])

    def test_actual_api_and_delivery_factories_close_real_router_on_server_constructor_failure(self):
        for factory, source in [(host_applications.api, 'kb_platform.api.create_app'),
                                (host_applications.delivery, 'kb_platform.delivery.create_delivery_app')]:
            with self.subTest(factory=factory.__name__):
                app, events = self.real_app()
                with patch(source, return_value=app), \
                        patch.object(host_applications, 'HttpApplication', side_effect=RuntimeError('server constructor')):
                    with self.assertRaisesRegex(RuntimeError, 'server constructor'):
                        factory({})
                self.assertEqual(events, ['sync-shutdown', 'async-shutdown'])

    def test_factory_cleanup_failure_preserves_constructor_error_in_exception_chain(self):
        app = FastAPI()
        def fail_shutdown():
            raise ValueError('cleanup failed')
        app.router.add_event_handler('shutdown', fail_shutdown)
        with patch('kb_platform.api.create_app', return_value=app), \
                patch.object(host_applications, 'HttpApplication', side_effect=RuntimeError('server constructor')):
            with self.assertRaisesRegex(ValueError, 'cleanup failed') as raised:
                host_applications.api({})
        self.assertIsInstance(raised.exception.__context__, RuntimeError)
        self.assertEqual(str(raised.exception.__context__), 'server constructor')
