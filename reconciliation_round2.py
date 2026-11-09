import json
from pathlib import Path

root = Path.cwd()
lock = root / 'web/package-lock.json'
before = json.loads(lock.read_text('utf-8'))
text = lock.read_text('utf-8')
count = text.count('https://registry.npmmirror.com/')
assert count > 0
text = text.replace('https://registry.npmmirror.com/', 'https://registry.npmjs.org/')
after = json.loads(text)
for name, entry in before['packages'].items():
    changed = dict(after['packages'][name])
    if 'resolved' in entry:
        assert changed['resolved'] == entry['resolved'].replace('https://registry.npmmirror.com/', 'https://registry.npmjs.org/')
        changed['resolved'] = entry['resolved']
    assert changed == entry, name
lock.write_text(text, encoding='utf-8')
print(f'Changed registry host for {count} locked tarballs; versions and integrity unchanged')
p = root / 'kb_platform/rag_testing/web_runner_contracts.py'
t = p.read_text('utf-8')
needle = '    def test_cli_rejects_existing_output_before_starting_services(self):'
assert needle in t
addition = '''    def test_workbench_verdict_requires_all_three_cases_and_assertions_in_each(self):
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

'''
p.write_text(t.replace(needle, addition + needle), encoding='utf-8')
