from contextlib import redirect_stdout
from io import StringIO
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from .browser_integration import BROWSER_ASSETS, browser_verdict, checked_browser_assets
from .browser_sources import ENTRIES, main, markup_violations, placement_violations
from .console_browser_integration import CASES, DRAFT_CASES, SCOPE_CASES, run_additional_cases, run_cases
from .formal_state_machines import digest, source_digest, state_candidates


ROOT = Path(__file__).resolve().parents[2]


class BrowserSourceContracts(unittest.TestCase):
    def test_browser_baseline_and_fixed_source_review_snapshots_are_distinct(self):
        baseline = json.loads((ROOT / 'docs/browser-state-baseline-v1.json').read_text('utf-8'))
        fixed = ROOT / 'docs/browser-state-fix-v1.json'
        review = json.loads((ROOT / 'docs/browser-state-integration-v2.json').read_text('utf-8'))
        self.assertEqual(review['previous_review_sha256'], sha256(fixed.read_bytes().replace(b'\r\n', b'\n')).hexdigest())
        originals = {row['path']: row['sha256'] for row in baseline['production_files_and_existing_cases']}
        originals.update({'e2e/' + name: row['source_sha256'] for name, row in baseline['new_suites'].items()})
        self.assertEqual(set(originals), {'kb_platform/agent_console.html', 'kb_platform/console.html',
            'kb_platform/console.mjs', 'e2e/knowledge.e2e.js', 'e2e/console-state.e2e.js',
            'e2e/agent-draft-state.e2e.js', 'e2e/console-scope-state.e2e.js'})
        changed = {row['path']: row for row in review['source_reviews']}
        self.assertEqual(set(changed), {'kb_platform/agent_console.html', 'kb_platform/console.mjs',
                                       'e2e/agent-draft-state.e2e.js', 'e2e/knowledge.e2e.js'})
        for path, old_hash in originals.items():
            current = sha256((ROOT / path).read_text('utf-8').encode('utf-8')).hexdigest()
            if path in changed:
                self.assertEqual(changed[path]['baseline_sha256'], old_hash)
                self.assertEqual(changed[path]['current_sha256'], current)
                self.assertTrue(changed[path]['review'])
            else:
                self.assertEqual(current, old_hash, path)
        self.assertEqual(set(review['added_sources']), {'kb_platform/agent_console.mjs'})
        for path, fingerprint in review['added_sources'].items():
            self.assertEqual(sha256((ROOT / path).read_text('utf-8').encode('utf-8')).hexdigest(), fingerprint)
        self.assertEqual(baseline['new_suites']['agent-draft-state.e2e.js']['cases'], 5)
        for name, cases in (('agent-draft-state.e2e.js', DRAFT_CASES), ('console-scope-state.e2e.js', SCOPE_CASES)):
            self.assertEqual(review['current_suite_counts'][name], len(cases))
        self.assertEqual(set(review['integration_sources']), {'web/src/operation-owner.ts', 'web/src/main.tsx',
            'web/src/workbench.tsx', 'e2e/workbench.e2e.js', 'e2e/web_chat.e2e.js', 'scripts/web_ownership.test.mjs'})
        for path, fingerprint in review['integration_sources'].items():
            self.assertEqual(sha256((ROOT / path).read_text('utf-8').encode()).hexdigest(), fingerprint, path)

    def test_budget_disclosure_has_one_real_summary_for_visible_fixture_checks(self):
        class Disclosure(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack, self.disclosures, self.summary = [], [], None

            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == 'details':
                    item = {'open': 'open' in values, 'runtime_nodes': 0, 'summaries': []}
                    self.stack.append(item)
                    self.disclosures.append(item)
                if self.stack and values.get('id') == 'runtimeStatus':
                    self.stack[-1]['runtime_nodes'] += 1
                if tag == 'summary' and self.stack:
                    self.summary = self.stack[-1]
                    self.summary['summaries'].append('')

            def handle_endtag(self, tag):
                if tag == 'summary':
                    self.summary = None
                if tag == 'details':
                    self.stack.pop()

            def handle_data(self, text):
                if self.summary is not None:
                    self.summary['summaries'][-1] += text

        parser = Disclosure()
        parser.feed((ROOT / 'kb_platform/agent_console.html').read_text('utf-8'))
        parser.close()
        targets = [item for item in parser.disclosures if item['runtime_nodes']]
        self.assertEqual(targets, [{'open': False, 'runtime_nodes': 1, 'summaries': ['服务状态与用量']}])
        self.assertEqual(parser.stack, [])

    def test_current_markup_has_one_explicit_mapped_owner(self):
        self.assertEqual(ENTRIES, {
            'kb_platform/console.html': ('kb_platform/console.mjs', '/console.mjs'),
            'kb_platform/agent_console.html': ('kb_platform/agent_console.mjs', '/agent_console.mjs'),
        })
        self.assertEqual([], placement_violations(ROOT))

    def test_both_entrypoints_reject_execution_bypasses_after_module_hash_refresh(self):
        for entrypoint, (module, url) in ENTRIES.items():
            for extra in ('<script>void 0</script>', '<button onclick="void 0">Run</button>',
                          '<iframe srcdoc="x"></iframe>', '<a href="javascript:void 0">Run</a>'):
                with self.subTest(entrypoint=entrypoint, extra=extra), TemporaryDirectory() as directory:
                    root = Path(directory)
                    for page, (script, _) in ENTRIES.items():
                        for name in (page, script):
                            target = root / name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text((ROOT / name).read_text('utf-8'), encoding='utf-8')
                    path = root / entrypoint
                    path.write_text(path.read_text('utf-8') + extra, encoding='utf-8')
                    binding = {'path': module, 'language': 'javascript-module', 'symbol': '<module>'}
                    binding['sha256'] = source_digest(root, binding)
                    self.assertEqual(binding['sha256'], source_digest(root, binding))
                    self.assertTrue(any(error.startswith(entrypoint + ':')
                                        for error in placement_violations(root)))

    def test_html_parser_accepts_equivalent_attribute_quoting_and_nonexecuting_text(self):
        source = "<p>script onclick javascript: are plain text</p><script src='/console.mjs' type=module></script>"
        self.assertEqual([], markup_violations(source, '/console.mjs'))

    def test_markup_cannot_add_an_unmapped_execution_path(self):
        valid = '<script type="module" src="/console.mjs"></script>'
        variants = ('', valid + valid, valid + '<script src="/other.js"></script>',
                    valid.replace('</script>', 'alert(1)</script>'),
                    valid.replace('type="module"', 'type="module" type="text/javascript"'),
                    valid.replace('</script>', ''), valid.replace('></script>', '/>'),
                    valid + '<button ONCLICK="run()">Run</button>', valid + '<iframe srcdoc="x"></iframe>',
                    valid + '<a href="java&#x09;script:run()">Run</a>',
                    valid + '<object data="data:text/html,x"></object>', valid + '<base href="https://other.invalid">',
                    valid + '<meta http-equiv="refresh" content="0;url=/other">')
        for source in variants:
            with self.subTest(source=source):
                self.assertTrue(markup_violations(source, '/console.mjs'))

    def test_missing_mapped_module_is_not_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entrypoint, (_, url) = next(iter(ENTRIES.items()))
            path = root / entrypoint
            path.parent.mkdir()
            path.write_text(f'<script type="module" src="{url}"></script>', encoding='utf-8')
            self.assertTrue(placement_violations(root))

    def test_javascript_binding_is_whole_module_review_drift_not_ast_equivalence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'owner.mjs'
            source = b'let generation = 0;\nexport const next = () => ++generation;\n'
            binding = {'path': 'owner.mjs', 'language': 'javascript-module', 'symbol': '<module>'}
            path.write_bytes(source)
            original = source_digest(root, binding)
            self.assertEqual(digest(source), original)
            path.write_bytes(source.replace(b'\n', b'\r\n'))
            self.assertEqual(original, source_digest(root, binding))
            path.write_bytes(source.replace(b'++generation', b'--generation'))
            self.assertNotEqual(original, source_digest(root, binding))
            with self.assertRaisesRegex(ValueError, 'complete .mjs module'):
                source_digest(root, {**binding, 'symbol': 'next'})
            self.assertIn(('owner.mjs', '<module>'), state_candidates(root, ['.']))

    def test_syntax_gate_requires_actual_checker_and_rejected_invalid_module(self):
        for invalid_code, invalid_error, expected in ((1, 'SyntaxError: invalid', 0), (0, '', 1), (2, 'unavailable', 1)):
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                if '--version' in command:
                    return CompletedProcess(command, 0, 'v22.23.1\n', '')
                if '--input-type=module' in command:
                    return CompletedProcess(command, invalid_code, '', invalid_error)
                return CompletedProcess(command, 0, '', '')

            with self.subTest(code=invalid_code, error=invalid_error), patch(
                    'kb_platform.rag_testing.browser_sources.shutil.which', return_value='/synthetic/node'), patch(
                    'kb_platform.rag_testing.browser_sources.subprocess.run', side_effect=run), redirect_stdout(StringIO()) as stream:
                self.assertEqual(expected, main())
                report = json.loads(stream.getvalue())
                self.assertEqual('v22.23.1', report['node_version'])
                self.assertEqual('export const = ;', calls[-1][1]['input'])
                self.assertTrue(all(row['source_sha256'] for row in report['syntax_checks']))
        with patch('kb_platform.rag_testing.browser_sources.shutil.which', return_value=None), redirect_stdout(StringIO()):
            self.assertEqual(1, main())

    def test_every_console_browser_case_is_mandatory_without_skips_or_retries(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'junit.xml'
            cases = [f'<testcase name="{name}" classname="console-state.e2e.js">{{}}</testcase>' for name in sorted(CASES)]
            valid = ''.join(case.format('') for case in cases)
            path.write_text('<testsuite>' + valid + '</testsuite>', encoding='utf-8')
            self.assertEqual(len(CASES), browser_verdict(path, expected=CASES, classname='console-state.e2e.js')['tests'])
            for altered in (valid.replace(cases[0].format(''), ''), valid + cases[0].format(''),
                            valid.replace('console-state.e2e.js', 'other.js'),
                            *(valid.replace(cases[0].format(''), cases[0].format(tag)) for tag in
                              ('<failure/>', '<error/>', '<skipped/>', '<system-out>flaky: 1 attempt</system-out>'))):
                path.write_text('<testsuite>' + altered + '</testsuite>', encoding='utf-8')
                with self.subTest(altered=altered), self.assertRaises(ValueError):
                    browser_verdict(path, expected=CASES, classname='console-state.e2e.js')


    def test_browser_asset_preflight_rejects_missing_stale_or_mistyped_modules(self):
        for broken in ('none', 'status', 'content', 'content-type', 'cache-control', 'x-content-type-options'):
            calls = []

            def get(url):
                calls.append(url)
                name, _ = next((name, media) for path, name, media in BROWSER_ASSETS
                               if url == 'http://fixture' + path)
                module = name.endswith('.mjs')
                content = (ROOT / 'kb_platform' / name).read_bytes()
                headers = {'content-type': 'text/javascript; charset=utf-8' if module else 'text/html; charset=utf-8',
                           'cache-control': 'no-store', 'x-content-type-options': 'nosniff'}
                if module and broken in headers:
                    headers[broken] = 'incorrect'
                return SimpleNamespace(status_code=404 if module and broken == 'status' else 200,
                    content=b'stale' if module and broken == 'content' else content, headers=headers)

            with self.subTest(broken=broken):
                client = SimpleNamespace(get=get)
                if broken == 'none':
                    report = checked_browser_assets(client, 'http://fixture')
                    self.assertEqual(200, report['/console.mjs']['status'])
                    self.assertEqual(64, len(report['/console.mjs']['sha256']))
                else:
                    with self.assertRaisesRegex(ValueError, 'Browser asset /console.mjs'):
                        checked_browser_assets(client, 'http://fixture')
                self.assertEqual(['http://fixture' + path for path, _, _ in BROWSER_ASSETS]
                                 if broken == 'none' else ['http://fixture/', 'http://fixture/console.mjs'], calls)


    def test_console_runner_uses_project_output_and_preserves_failed_run_artifacts(self):
        for returncode in (0, 2):
            with self.subTest(returncode=returncode), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'e2e').mkdir()
                (root / 'kb_platform').mkdir()
                for name in ('console.html', 'console.mjs'):
                    (root / 'kb_platform' / name).write_text('synthetic source', encoding='utf-8')
                output = root / 'reports/console'

                def run(command, **kwargs):
                    target = Path(command[command.index('--output') + 1])
                    self.assertTrue(target.is_relative_to(root / 'e2e'))
                    self.assertEqual(root / 'e2e', kwargs['cwd'])
                    self.assertEqual('console-state.e2e.js', kwargs['env']['KB_E2E_TESTS'])
                    target.mkdir(parents=True)
                    (target / 'partial.txt').write_text('preserve even on failure', encoding='utf-8')
                    document = ET.Element('testsuite')
                    for name in CASES:
                        ET.SubElement(document, 'testcase', name=name, classname='console-state.e2e.js')
                    ET.ElementTree(document).write(target / 'junit.xml')
                    (target / 'report.json').write_text(json.dumps({'run': {'status': 'passed', 'exitCode': 0,
                        'errors': [], 'usage': {'discoveredResults': len(CASES),
                                              'modelTokens': 0, 'maxModelCallsInStep': 0}}}), encoding='utf-8')
                    return CompletedProcess(command, returncode)

                with patch('kb_platform.rag_testing.console_browser_integration.ROOT', root), patch(
                        'kb_platform.rag_testing.console_browser_integration.shutil.which', return_value='/synthetic/node'), patch(
                        'kb_platform.rag_testing.console_browser_integration.subprocess.run', side_effect=run):
                    if returncode:
                        with self.assertRaisesRegex(RuntimeError, 'failed with exit 2'):
                            run_cases(output, {})
                    else:
                        self.assertEqual(len(CASES), run_cases(output, {})['tests'])
                self.assertEqual('preserve even on failure', (output / 'browser/partial.txt').read_text('utf-8'))
                observed = json.loads((output / 'report.json').read_text('utf-8'))
                self.assertEqual(not returncode, observed['usage_observed'])
                self.assertEqual(None if returncode else 0, observed['new_model_calls'])

    def test_case_usage_is_observed_on_assertion_failure_but_never_invented(self):
        for mode in ('assertion', 'missing', 'partial', 'nonzero', 'infrastructure'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'e2e').mkdir()
                output = root / 'reports/suite'

                def run(command, **kwargs):
                    target = Path(command[command.index('--output') + 1])
                    target.mkdir(parents=True)
                    code = 1 if mode in {'assertion', 'missing'} else 0
                    usage = {'discoveredResults': len(DRAFT_CASES), 'modelTokens': 0, 'maxModelCallsInStep': 0}
                    if mode == 'partial':
                        usage.pop('maxModelCallsInStep')
                    if mode == 'nonzero':
                        usage['modelTokens'] = 3
                    if mode != 'missing':
                        (target / 'report.json').write_text(json.dumps({'run': {
                            'status': 'failed' if code else 'passed', 'exitCode': code,
                            'errors': ['synthetic infrastructure error'] if mode == 'infrastructure' else [],
                            'usage': usage}}), 'utf-8')
                    return CompletedProcess(command, code)

                with patch('kb_platform.rag_testing.console_browser_integration.ROOT', root), patch(
                        'kb_platform.rag_testing.console_browser_integration.shutil.which', return_value='/synthetic/node'), patch(
                        'kb_platform.rag_testing.console_browser_integration.subprocess.run', side_effect=run):
                    with self.assertRaises(RuntimeError):
                        run_cases(output, {}, tests='agent-draft-state.e2e.js', expected=DRAFT_CASES,
                                  sources=('agent_console.html',))
                report = json.loads((output / 'report.json').read_text('utf-8'))
                self.assertEqual('FAIL', report['status'])
                self.assertEqual(mode in {'assertion', 'nonzero'}, report['usage_observed'])
                self.assertEqual(mode == 'assertion', report['zero_model_usage_verified'])
                self.assertEqual(0 if mode == 'assertion' else None, report['new_model_calls'])
                if mode not in {'assertion', 'nonzero'}:
                    self.assertIsNone(report['browser_usage'])

    def test_agent_asset_preflight_requires_exact_current_public_page(self):
        for broken in ('status', 'content', 'content-type'):
            def get(url):
                name, _ = next((name, media) for path, name, media in BROWSER_ASSETS
                               if url == 'http://fixture' + path)
                agent = name == 'agent_console.html'
                content = (ROOT / 'kb_platform' / name).read_bytes()
                media_type = 'text/javascript' if name.endswith('.mjs') else 'text/html'
                return SimpleNamespace(status_code=404 if agent and broken == 'status' else 200,
                    content=b'stale agent page' if agent and broken == 'content' else content,
                    headers={'content-type': 'incorrect' if agent and broken == 'content-type' else media_type,
                             'cache-control': 'no-store', 'x-content-type-options': 'nosniff'})

            with self.subTest(broken=broken), self.assertRaisesRegex(ValueError, 'Browser asset /agent'):
                checked_browser_assets(SimpleNamespace(get=get), 'http://fixture')

    def test_every_new_state_case_is_required_without_skips_or_retries(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'junit.xml'
            for classname, expected in (('agent-draft-state.e2e.js', DRAFT_CASES),
                                        ('console-scope-state.e2e.js', SCOPE_CASES)):
                cases = [f'<testcase name="{name}" classname="{classname}">{{}}</testcase>'
                         for name in sorted(expected)]
                valid = ''.join(case.format('') for case in cases)
                path.write_text('<testsuite>' + valid + '</testsuite>', 'utf-8')
                self.assertEqual(len(expected), browser_verdict(path, expected=expected, classname=classname)['tests'])
                for altered in (valid.replace(cases[0].format(''), ''), valid + cases[0].format(''),
                                *(valid.replace(cases[0].format(''), cases[0].format(tag)) for tag in
                                  ('<failure/>', '<error/>', '<skipped/>', '<system-out>flaky: 1 attempt</system-out>'))):
                    path.write_text('<testsuite>' + altered + '</testsuite>', 'utf-8')
                    with self.subTest(classname=classname, altered=altered), self.assertRaises(ValueError):
                        browser_verdict(path, expected=expected, classname=classname)

    def test_additional_runner_observes_both_suites_even_when_first_fails(self):
        for fail in ('none', 'observed', 'missing'):
            with self.subTest(fail=fail), TemporaryDirectory() as directory:
                output = Path(directory) / 'additional'
                calls = []

                def run(path, env, **kwargs):
                    calls.append((path.name, kwargs))
                    detail = {'tests': len(kwargs['expected']), 'usage_observed': True,
                              'zero_model_usage_verified': True, 'new_model_calls': 0}
                    if fail != 'none' and path.name == 'agent-draft':
                        if fail == 'observed':
                            path.mkdir()
                            (path / 'report.json').write_text(json.dumps(detail), 'utf-8')
                        raise RuntimeError('synthetic browser assertion failure')
                    return detail

                with patch('kb_platform.rag_testing.console_browser_integration.run_cases', side_effect=run):
                    if fail != 'none':
                        with self.assertRaisesRegex(RuntimeError, 'Additional browser state regressions failed'):
                            run_additional_cases(output, {})
                    else:
                        self.assertEqual('PASS', run_additional_cases(output, {})['status'])
                self.assertEqual(['agent-draft', 'console-scope'], [name for name, _ in calls])
                report = json.loads((output / 'report.json').read_text('utf-8'))
                self.assertEqual('PASS' if fail == 'none' else 'FAIL', report['status'])
                self.assertEqual('PASS', report['suites']['console-scope']['status'])
                self.assertEqual(fail != 'missing', report['usage_observed'])
                self.assertEqual(0 if fail != 'missing' else None, report['new_model_calls'])
                self.assertEqual(DRAFT_CASES, calls[0][1]['expected'])
                self.assertEqual(SCOPE_CASES, calls[1][1]['expected'])
