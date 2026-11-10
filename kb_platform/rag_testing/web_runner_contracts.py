import json
from pathlib import Path
import contextlib
import io
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from e2e.run_web_chat import browser_verdict, arguments


class WebRunnerContracts(unittest.TestCase):
    def test_verdict_requires_actual_assertions_and_no_skips_retries_or_model_calls(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            report = {'run': {'status': 'passed', 'exitCode': 0, 'errors': [],
                'usage': {'modelTokens': 0, 'maxModelCallsInStep': 0},
                'results': [{'file': 'web_chat.e2e.js', 'status': 'passed', 'attempts': [
                    {'status': 'passed', 'steps': [{'kind': 'assertion', 'status': 'passed'}]}]}]}}
            def verdict(value):
                (output / 'report.json').write_text(json.dumps(value), encoding='utf-8')
                return browser_verdict(output, 'web_chat.e2e.js')
            self.assertEqual(verdict(report)['assertions'], 1)
            for mutation in ('empty', 'skip', 'retry', 'model', 'no_assertions', 'failed_assertion'):
                with self.subTest(mutation=mutation):
                    value = json.loads(json.dumps(report))
                    run = value['run']
                    if mutation == 'empty':
                        run['results'] = []
                    elif mutation == 'skip':
                        run['results'][0]['status'] = 'skipped'
                    elif mutation == 'retry':
                        run['results'][0]['attempts'] *= 2
                    elif mutation == 'model':
                        run['usage']['maxModelCallsInStep'] = 1
                    elif mutation == 'no_assertions':
                        run['results'][0]['attempts'][0]['steps'] = []
                    else:
                        run['results'][0]['attempts'][0]['steps'][0]['status'] = 'failed'
                    with self.assertRaises(ValueError):
                        verdict(value)

    def test_workbench_verdict_requires_all_three_cases_and_assertions_in_each(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            case = {'file': 'workbench.e2e.js', 'status': 'passed', 'attempts': [
                {'status': 'passed', 'steps': [{'kind': 'assertion', 'status': 'passed'}]}]}
            report = {'run': {'status': 'passed', 'exitCode': 0, 'errors': [],
                'usage': {'modelTokens': 0, 'maxModelCallsInStep': 0},
                'results': [json.loads(json.dumps(case)) for _ in range(3)]}}
            def verdict(value, count=3):
                (output / 'report.json').write_text(json.dumps(value), encoding='utf-8')
                return browser_verdict(output, 'workbench.e2e.js', count)
            self.assertEqual(verdict(report)['tests'], 3)
            self.assertEqual(verdict(report)['assertions'], 3)
            for mutation in ('missing', 'extra', 'other_file', 'empty_case', 'failed_case', 'retry', 'zero_expected'):
                with self.subTest(mutation=mutation):
                    value = json.loads(json.dumps(report))
                    results = value['run']['results']
                    if mutation == 'missing':
                        results.pop()
                    elif mutation == 'extra':
                        results.append(case)
                    elif mutation == 'other_file':
                        results[1]['file'] = 'unrelated.e2e.js'
                    elif mutation == 'empty_case':
                        results[1]['attempts'][0]['steps'] = []
                    elif mutation == 'failed_case':
                        results[1]['attempts'][0]['steps'][0]['status'] = 'failed'
                    elif mutation == 'retry':
                        results[1]['attempts'] *= 2
                    with self.assertRaises(ValueError):
                        verdict(value, 0 if mutation == 'zero_expected' else 3)

    def test_cli_rejects_existing_output_before_starting_services(self):
        with TemporaryDirectory() as directory:
            stderr = io.StringIO()
            argv = ['runner', '--output', directory, '--checker-summary',
                    str(Path(directory) / 'missing.json')]
            with patch('sys.argv', argv), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    arguments('Browser acceptance')
            self.assertEqual(caught.exception.code, 2)
            self.assertIn('--output must be a new directory', stderr.getvalue())
