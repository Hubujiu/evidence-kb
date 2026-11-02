import json
from types import SimpleNamespace
import unittest

from kb_platform.authentication import SessionAuthentication, StaticAuthentication
from kb_platform.plugin_host import PluginSpec
from kb_platform.session_identity import Actor
from kb_platform.storage_sdk import StorageError


class MemorySessions:
    def __init__(self):
        self.values = {}
        self.available = True

    def get(self, key):
        if not self.available:
            raise StorageError('STORAGE_BACKEND_UNAVAILABLE')
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def put_expiring(self, key, value, *, ttl_seconds):
        self.values[key] = value

    def delete(self, key):
        return self.values.pop(key, None) is not None


class AuthenticationContracts(unittest.TestCase):
    def test_expiry_revocation_and_tokens_are_never_stored_in_plaintext(self):
        store = MemorySessions()
        clock = [100]
        auth = SessionAuthentication(store, clock=lambda: clock[0])
        actor = Actor('tenant', 'alice')
        session = auth.issue(actor, ttl_seconds=10)
        self.assertEqual(auth.authenticate(session.token), actor)
        self.assertNotIn(session.token, json.dumps(store.values, default=lambda value: value.decode()))
        clock[0] = 110
        self.assertIsNone(auth.authenticate(session.token))
        clock[0] = 100
        self.assertTrue(auth.revoke(session.token))
        self.assertIsNone(auth.authenticate(session.token))
        self.assertIsNone(auth.authenticate('短' * 40))
        self.assertIsNone(auth.authenticate('short'))
        self.assertFalse(auth.revoke('invalid'))

    def test_corrupt_sessions_and_backend_errors_fail_closed(self):
        store = MemorySessions()
        auth = SessionAuthentication(store)
        session = auth.issue(Actor('tenant', 'alice'))
        key = next(iter(store.values))
        for corrupt in (b'{', b'null', b'[]', b'NaN', b'{"version": true}', b'\xff'):
            store.values[key] = corrupt
            with self.assertRaisesRegex(StorageError, 'AUTH_SESSION_INVALID'):
                auth.authenticate(session.token)
        store.available = False
        with self.assertRaisesRegex(StorageError, 'STORAGE_BACKEND_UNAVAILABLE'):
            auth.authenticate(session.token)
        for ttl in (0, -1, True, float('nan'), float('inf'), 365 * 86400 + 1):
            with self.assertRaisesRegex(ValueError, 'AUTH_INVALID_TTL'):
                auth.issue(Actor('tenant', 'alice'), ttl_seconds=ttl)

    def test_file_authentication_remains_available_through_same_port(self):
        token = 'synthetic-token-' * 4
        actor = Actor('tenant', 'alice')
        auth = StaticAuthentication({token: actor})
        self.assertEqual(auth.authenticate(token), actor)
        self.assertIsNone(auth.authenticate('x' * 40))
        self.assertIsNone(auth.authenticate('非ASCII' * 40))

    def test_invalid_authentication_provider_is_rejected_and_cleaned_up(self):
        from kb_platform.application_composition import create_root_application

        closed = []

        def mount(ctx, config):
            ctx.defer(lambda: closed.append(True))
            ctx.provide('authentication', object())

        spec = PluginSpec('broken-auth', ('authentication',), (), mount)
        with self.assertRaisesRegex(ValueError, 'AUTHENTICATION_PROTOCOL_INVALID'):
            create_root_application(SimpleNamespace(), profile={'version': 1, 'plugins': [
                {'id': 'auth', 'use': 'broken-auth'}]}, environment={}, extra_plugins=(spec,),
                authentication=True)
        self.assertEqual(closed, [True])


def live_suite():
    import asyncio
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import patch

    import httpx

    from kb_platform.storage_sdk import KV_EXPIRING_V1, StorageExport, StorageIdentity, StorageProvider

    class AuthenticationIntegration(unittest.TestCase):
        def test_http_uses_replaceable_ttl_store_without_reading_identity_file(self):
            store = MemorySessions()
            closed = []

            class Provider:
                def exports(self):
                    return (StorageExport('sessions', (KV_EXPIRING_V1,), self.open_space),)

                def open_space(self, space, target):
                    store.identity = StorageIdentity(space, target.cache_id)
                    return store

                def close(self):
                    closed.append(True)

            configuration = {'version': 2, 'application_id': 'auth-tests',
                'instances': {'sessions': {'provider': 'synthetic-ttl', 'config': {}}},
                'services': {'sessions': {'instance': 'sessions', 'export': 'sessions'}},
                'bindings': {'authentication': {'sessions': {'service': 'sessions', 'owner': 'auth',
                    'space': 'sessions', 'cache_id': 'test-sessions'}}}}
            profile = {'version': 1, 'plugins': [
                {'id': 'platform', 'use': 'platform.borrowed'},
                {'id': 'authentication', 'use': 'authentication.sessions'}]}
            with TemporaryDirectory() as directory, patch.dict('os.environ', {'KB_DEFER_DEFAULT_APP': '1'}):
                from kb_platform.api import create_app

                def forbidden():
                    raise AssertionError('session authentication must not read file identities')

                settings = SimpleNamespace(storage_root=Path(directory), identities=forbidden)
                app = create_app(settings, platform=SimpleNamespace(embedding_provider=None, ocr_client=None),
                    profile=profile, storage_configuration=configuration,
                    provider_catalog={'synthetic-ttl': StorageProvider(lambda config: Provider(),
                        {'sessions': (KV_EXPIRING_V1,)})})
                host = app.state.root_plugins
                try:
                    session = host.call('authentication', 'issue', Actor('tenant', 'alice'))

                    async def requests():
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
                            headers = {'Authorization': 'Bearer ' + session.token}
                            response = await client.get('/identity', headers=headers)
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(response.json(), {'tenant_id': 'tenant', 'principal_id': 'alice'})
                            store.available = False
                            self.assertEqual((await client.get('/identity', headers=headers)).status_code, 503)
                            store.available = True
                            host.call('authentication', 'revoke', session.token)
                            self.assertEqual((await client.get('/identity', headers=headers)).status_code, 401)
                            self.assertEqual((await client.get('/identity')).status_code, 401)

                    asyncio.run(requests())
                finally:
                    host.close()
            self.assertEqual(closed, [True])

    return unittest.defaultTestLoader.loadTestsFromTestCase(AuthenticationIntegration)
