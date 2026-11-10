import asyncio
import ast
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from .browser_integration import BROWSER_ASSETS, checked_browser_assets
from .browser_sources import controlled_stage_violations


ROOT = Path(__file__).resolve().parents[2]
ASSET_CASE_ID = ('kb_platform.rag_testing.browser_owner_contracts.live_suite.<locals>.'
                 'BrowserAssetIntegration.test_host_application_serves_actual_modules_without_auth_but_identity_stays_protected')


def text_asset_bytes(path):
    return path.read_bytes().replace(b'\r\n', b'\n')


class BrowserOwnerContracts(unittest.TestCase):
    def test_served_text_allows_only_checkout_crlf_normalization(self):
        original = b'export const value = 1;\n'
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'asset.mjs'
            for source in (original, original.replace(b'\n', b'\r\n')):
                path.write_bytes(source)
                self.assertEqual(text_asset_bytes(path), original)
            for source in (original.replace(b'1', b'2'), original.replace(b'\n', b'\r'), original + b' '):
                path.write_bytes(source)
                self.assertNotEqual(text_asset_bytes(path), original)

    def test_agent_module_preflight_rejects_missing_stale_or_incorrect_headers(self):
        for broken in ('status', 'content', 'content-type', 'cache-control', 'x-content-type-options'):
            def get(url):
                name, media = next((name, media) for path, name, media in BROWSER_ASSETS
                                   if url == 'http://fixture' + path)
                module = name == 'kb_platform/agent_console.mjs'
                headers = {'content-type': media, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff'}
                if module and broken in headers:
                    headers[broken] = 'incorrect'
                return SimpleNamespace(status_code=404 if module and broken == 'status' else 200,
                    content=b'stale' if module and broken == 'content' else (ROOT / name).read_bytes(),
                    headers=headers)

            with self.subTest(broken=broken), self.assertRaisesRegex(ValueError, 'Browser asset /agent_console.mjs'):
                checked_browser_assets(SimpleNamespace(get=get), 'http://fixture')

    def test_public_asset_case_is_required_once_in_the_controlled_stage(self):
        from .foundation_integration import case_ids, integration_suite

        suite = integration_suite()
        self.assertEqual(list(case_ids(suite)).count(ASSET_CASE_ID), 1)
        offline = unittest.defaultTestLoader.loadTestsFromTestCase(BrowserOwnerContracts)
        self.assertNotIn(ASSET_CASE_ID, {case.id() for case in offline})
        for replacement in (unittest.TestSuite(), unittest.TestSuite([live_suite(), live_suite()])):
            with self.subTest(replacement=replacement.countTestCases()), patch(
                    'kb_platform.rag_testing.browser_owner_contracts.live_suite', return_value=replacement):
                with self.assertRaisesRegex(ValueError, 'exactly one required browser asset case'):
                    integration_suite()

    def test_controlled_asset_stage_registration_cannot_be_removed_or_replaced(self):
        checker = (ROOT / 'scripts/check_project.py').read_text('utf-8')
        runner = (ROOT / 'kb_platform/rag_testing/foundation_integration.py').read_text('utf-8')
        self.assertEqual(controlled_stage_violations(checker, runner), [])
        for name in ('run_check', 'CheckResult'):
            class RemoveStage(ast.NodeTransformer):
                def visit_Expr(self, node):
                    calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
                    if any(isinstance(call.func, ast.Name) and call.func.id == name and call.args
                           and isinstance(call.args[0], ast.Constant)
                           and call.args[0].value == 'foundation-integration' for call in calls):
                        return ast.Pass()
                    return node

            changed = ast.unparse(ast.fix_missing_locations(RemoveStage().visit(ast.parse(checker))))
            with self.subTest(removed=name):
                self.assertTrue(controlled_stage_violations(changed, runner))
        parsed = ast.parse(runner)
        for node in ast.walk(parsed):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'integration_suite':
                node.func.id = 'unvalidated_suite'
        self.assertTrue(controlled_stage_violations(checker, ast.unparse(parsed)))


def live_suite():
    class BrowserAssetIntegration(unittest.TestCase):
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
                                self.assertEqual(response.content, text_asset_bytes(ROOT / name), path)
                                self.assertEqual(response.headers['content-type'].split(';')[0], media)
                                self.assertEqual(response.headers['cache-control'], 'no-store')
                                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                            self.assertEqual((await client.get('/identity')).status_code, 401)

                asyncio.run(requests())

    return unittest.defaultTestLoader.loadTestsFromTestCase(BrowserAssetIntegration)
