"""Structural semantic obligations for the canonical public-library workflow."""
import ast


def _dump(value):
    return ast.dump(value, include_attributes=False)


def _expression(source):
    return _dump(ast.parse(source, mode='eval').body)


def _calls(node, name):
    return [item for item in ast.walk(node) if isinstance(item, ast.Call)
            and ((isinstance(item.func, ast.Attribute) and item.func.attr == name)
                 or (isinstance(item.func, ast.Name) and item.func.id == name))]


def canonical_page_violations(source):
    try:
        root = ast.parse(source)
        owner = next(node for node in root.body if isinstance(node, ast.ClassDef)
                     and node.name == 'CanonicalPageIngestion')
        methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}
        run, receipt = methods['run'], methods['_receipt']
        errors = []
        # Validation must be a disjunction that directly raises: merely retaining
        # the comparison in dead code, a tuple or an AND cannot satisfy the gate.
        guards = [node for node in ast.walk(receipt) if isinstance(node, ast.If)
                  and isinstance(node.test, ast.BoolOp) and isinstance(node.test.op, ast.Or)
                  and any(isinstance(child, ast.Raise) for child in node.body)]
        clauses = [{_dump(value) for value in guard.test.values} for guard in guards]
        required = {
            'result.get("request_id") != request_id', 'result.get("physical_request_id") != request_id',
            'result.get("task") != "document"', 'result.get("status") != "COMPLETE"',
            'result.get("texts") != texts', 'result.get("audit_context") != context',
            'result.get("manifest_sha256") != digest(self.manifest)',
            'result.get("provider_manifest") != self.manifest',
            'result.get("vectors_sha256") != digest(vectors)', 'row["status"] != "COMPLETE"',
            'row["usage"] != result.get("usage")', 'row["reserved"] != result.get("reserved")',
        }
        if not any({_expression(value) for value in required} <= guard for guard in clauses):
            errors.append('canonical receipt identity/accounting must reject through direct OR guard')
        output_guard = [node for node in run.body if isinstance(node, ast.If)
                        and _dump(node.test) == _expression('output.exists()')
                        and any(isinstance(child, ast.Raise) for child in node.body)]
        mkdir = _calls(run, 'mkdir')
        if len(output_guard) != 1 or len(mkdir) != 1 or output_guard[0].lineno >= mkdir[0].lineno:
            errors.append('canonical output must reject existing directories before creating a run')
        serial = ('documents_observed', '_receipt', 'writer', 'import_capsule')
        calls = {name: _calls(run, name) for name in serial}
        if any(len(values) != 1 for values in calls.values()) or any(
                calls[left][0].lineno >= calls[right][0].lineno for left, right in zip(serial, serial[1:])):
            errors.append('canonical send/receipt/write/readback sequence must remain ordered')
        batch = next(node for node in ast.walk(run) if isinstance(node, ast.For)
                     and isinstance(node.target, ast.Name) and node.target.id == 'offset')
        auth_guards = [node for node in batch.body if isinstance(node, ast.If)
                       and _dump(node.test) == _expression('not snapshot["authorize"]()')
                       and any(isinstance(child, ast.Raise) for child in node.body)]
        if (len(auth_guards) != 2 or not calls['documents_observed']
                or not auth_guards[0].lineno < calls['documents_observed'][0].lineno < auth_guards[1].lineno):
            errors.append('canonical per-batch authorization must bracket actual encoding')
        readback_guard = [node for node in ast.walk(run) if isinstance(node, ast.If)
                          and _dump(node.test) == _expression(
                              '[list(v) for v in imported.vectors] != vectors or '
                              'imported.manifest.get("new_embedding_calls") != 0')
                          and any(isinstance(child, ast.Raise) for child in node.body)]
        completion = [node for node in ast.walk(run) if isinstance(node, ast.Assign)
                      and any(ast.unparse(target) == 'capsules[source_id]' for target in node.targets)]
        if (len(readback_guard) != 1 or len(completion) != 1
                or not calls['import_capsule'] or not calls['import_capsule'][0].lineno
                < readback_guard[0].lineno < completion[0].lineno):
            errors.append('canonical completed capsule requires verified zero-call readback')
        outer = next(node for node in run.body if isinstance(node, ast.Try))
        handlers = outer.handlers
        if len(handlers) != 1 or any(_calls(handlers[0], name) for name in
                                    ('finish', 'reserve', 'documents_observed')):
            errors.append('canonical failure reporting cannot settle or replay physical work')
        if len(handlers) == 1:
            texts = {_dump(node) for node in ast.walk(handlers[0])}
            if not {_expression('self.ledger.summary()'), _expression('_registered(self.ledger, request_ids)'),
                    _expression('_remote_calls(self.ledger, request_ids)')} <= texts:
                errors.append('canonical failure report must retain original physical evidence')
        aggregate = [call for call in _calls(run, '_write') if call.args
                     and _dump(call.args[0]) == _expression('output / "capsules.json"')]
        build = _calls(run, 'build')
        result = [call for call in _calls(run, '_write') if call.args
                  and _dump(call.args[0]) == _expression('output / "result.json"')]
        if (len(aggregate) != 1 or len(build) != 1 or len(result) != 2
                or not aggregate[0].lineno < build[0].lineno < min(item.lineno for item in result)):
            errors.append('canonical aggregate/build/result publication ordering changed')
        return errors
    except (ValueError, StopIteration, KeyError, SyntaxError, AttributeError) as error:
        return ['canonical workflow structure missing: ' + type(error).__name__]
