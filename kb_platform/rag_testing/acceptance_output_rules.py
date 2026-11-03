import ast
from pathlib import Path


DELIVERY = 'kb_platform/rag_testing/delivery_http.py'
RUNNER = 'kb_platform/rag_testing/runner.py'


def acceptance_output_violations(root):
    errors = []
    try:
        source = ast.parse((Path(root) / DELIVERY).read_text('utf-8'))
        functions = {node.name: node for node in source.body if isinstance(node, ast.FunctionDef)}
        publish = functions['_publish_bound_json']
        expected = ast.parse("""def publish(path, value):
    path = Path(path)
    if path.exists():
        if json.loads(path.read_text('utf-8')) != value:
            raise ValueError('BOUND_PREPARE_ARTIFACT_CHANGED:' + path.name)
        return
    _atomic_json(path, value)
""").body[0]
        if [ast.dump(node) for node in publish.body] != [ast.dump(node) for node in expected.body]:
            errors.append('acceptance_output: existing binding artifacts must be equal and read-only or reject')
        bind = functions['_bind_prepared_http']
        statements = [ast.unparse(node) for node in bind.body]
        calls = [ast.unparse(node) for node in ast.walk(bind) if isinstance(node, ast.Call)]
        required = ("_publish_bound_json(root / name, value)",
                    "_publish_bound_json(root / 'scenario-inputs.json', scenarios)",
                    "_publish_bound_json(root / 'prepared.json', prepared)")
        if any(calls.count(value) != 1 for value in required) or any(value.startswith('write_json(') for value in calls):
            errors.append('acceptance_output: every split/scenario/descriptor uses immutable publication')
        if statements[-4:] != [required[-1], "state['status'] = 'COMPLETE'", '_atomic_json(state_path, state)', 'return prepared']:
            errors.append('acceptance_output: COMPLETE must follow descriptor publication')
        final = next(node for node in bind.body if isinstance(node, ast.For) and ast.unparse(node.iter) == 'before.items()')
        if (ast.unparse(final.body[0]) != "_assert_authority(read_authority(client, data_scopes[split]['scope_id']), expected)"
                or bind.body.index(final) >= next(i for i, node in enumerate(bind.body)
                    if ast.unparse(node) == required[1])):
            errors.append('acceptance_output: final authority observations precede publication')
        adapter = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'DeliveryHTTPAdapter')
        execute = next(node for node in adapter.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
        text = ast.unparse(execute)
        if text.count('require_no_pending(self.physical_directory)') != 2:
            errors.append('acceptance_output: pending physical settlement gates case and turn admission')
        if "record['context'].get('turn_id') in turn_ids" not in text or text.count('**_usage(model_records)') != 2:
            errors.append('acceptance_output: success and exception traces retain all prior physical calls')
        caught = next(node for node in execute.body if isinstance(node, ast.Try)).handlers[0]
        if not any(isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'model_records'
                for target in node.targets) for node in ast.walk(caught)):
            errors.append('acceptance_output: exception handling must recover physical accounting')
        run_tree = ast.parse((Path(root) / RUNNER).read_text('utf-8'))
        run = next(node for node in run_tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
        text = ast.unparse(run)
        sequence = ('output.mkdir(parents=True, exist_ok=False)', 'for case in suite.cases:',
                    'traces.append(trace)', 'reports(output, report)', "write_json(output / 'manifest.json', files)",
                    "write_json(output / 'state.json', {'status': 'COMPLETE', 'decision': report['decision']})")
        positions = [text.find(value) for value in sequence]
        if -1 in positions or positions != sorted(positions):
            errors.append('acceptance_output: fixed cases and all traces/reports/manifest must precede COMPLETE in exclusive output')
    except (OSError, SyntaxError, KeyError, StopIteration, IndexError, ValueError) as error:
        errors.append('acceptance_output: invalid workflow structure: ' + type(error).__name__)
    return errors
