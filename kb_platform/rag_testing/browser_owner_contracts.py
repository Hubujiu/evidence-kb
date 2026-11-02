import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from .browser_integration import BROWSER_ASSETS, checked_browser_assets


ROOT = Path(__file__).resolve().parents[2]


class BrowserOwnerContracts(unittest.TestCase):
    def test_agent_module_preflight_rejects_missing_stale_or_incorrect_headers(self):
        for broken in ('status', 'content', 'content-type', 'cache-control', 'x-content-type-options'):
            def get(url):
                name, media = next((name, media) for path, name, media in BROWSER_ASSETS
                                   if url == 'http://fixture' + path)
                module = name == 'agent_console.mjs'
                headers = {'content-type': media, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff'}
                if module and broken in headers:
                    headers[broken] = 'incorrect'
                return SimpleNamespace(status_code=404 if module and broken == 'status' else 200,
                    content=b'stale' if module and broken == 'content' else (ROOT / 'kb_platform' / name).read_bytes(),
                    headers=headers)

            with self.subTest(broken=broken), self.assertRaisesRegex(ValueError, 'Browser asset /agent_console.mjs'):
                checked_browser_assets(SimpleNamespace(get=get), 'http://fixture')

    def test_host_application_serves_actual_modules_without_auth_but_identity_stays_protected(self):
        from kb_platform.api import create_app
        from kb_platform.host_applications import api
        from kb_platform.session_identity import Actor

        profile = {'version': 1, 'plugins': [{'id': 'platform', 'use': 'platform.borrowed'}]}
        with TemporaryDirectory() as directory:
            settings = SimpleNamespace(storage_root=Path(directory),
                identities=lambda: {'synthetic-browser-token-' * 4: Actor('fixture', 'owner')})
            platform = SimpleNamespace(embedding_provider=None, ocr_client=None)

            def construct(*, profile):
                return create_app(settings=settings, platform=platform, profile=profile)

            with patch('kb_platform.api.create_app', side_effect=construct):
                application = api({'profile': profile, 'host': '127.0.0.1', 'port': 0, 'log_level': 'warning'})

            async def requests():
                async with application.app.router.lifespan_context(application.app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app),
                            base_url='http://fixture', trust_env=False) as client:
                        for path, name, media in BROWSER_ASSETS:
                            response = await client.get(path)
                            self.assertEqual(response.status_code, 200, path)
                            self.assertEqual(response.content, (ROOT / 'kb_platform' / name).read_bytes(), path)
                            self.assertEqual(response.headers['content-type'].split(';')[0], media)
                            self.assertEqual(response.headers['cache-control'], 'no-store')
                            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                        self.assertEqual((await client.get('/identity')).status_code, 401)

            asyncio.run(requests())
