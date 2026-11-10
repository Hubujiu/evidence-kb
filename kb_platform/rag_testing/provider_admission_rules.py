import ast
from pathlib import Path


def _shape(node):
    return ast.dump(node, include_attributes=False)


def _function(root, relative, qualified):
    node = ast.parse((Path(root) / relative).read_text('utf-8'))
    for name in qualified.split('.'):
        node = next(child for child in node.body if isinstance(child, (ast.ClassDef, ast.FunctionDef))
                    and child.name == name)
    return node


def _nodes(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _nodes(child)


def _contains(node, source):
    expected = _shape(ast.parse(source).body[0])
    return next((child for child in _nodes(node) if _shape(child) == expected), None)


def _calls(node, name):
    return [child for child in _nodes(node) if isinstance(child, ast.Call)
            and ast.unparse(child.func) == name]


def _guard(node, condition, code):
    expected = _shape(ast.parse(condition, mode='eval').body)
    return next((child for child in _nodes(node) if isinstance(child, ast.If)
                 and _shape(child.test) == expected and not child.orelse and len(child.body) == 1
                 and isinstance(child.body[0], ast.Raise) and any(isinstance(value, ast.Constant)
                     and value.value == code for value in ast.walk(child.body[0]))), None)


def provider_admission_violations(root):
    errors = []

    def require(value, message):
        if not value:
            errors.append('provider_admission: ' + message)

    try:
        wrapper = _function(root, 'kb_platform/model_http.py', 'OpenAICompatibleAdapter.invoke')
        require(any(isinstance(child, ast.With)
                    and any(ast.unparse(item.context_expr) == 'self.ledger.admission()' for item in child.items)
                    and len(_calls(child, 'self._invoke_admitted')) == 1 for child in _nodes(wrapper)),
                'healthy in-flight ownership must remain held across invocation')
        invoke = _function(root, 'kb_platform/model_http.py', 'OpenAICompatibleAdapter._invoke_admitted')
        send = _calls(invoke, 'self.client.send')[0]
        block = _calls(invoke, 'self.admission.block')[0]
        settlements = _calls(invoke, 'self.ledger.finish')
        settlement = settlements[0]
        response_guard = next(child for child in _nodes(invoke) if isinstance(child, ast.If)
                              and ast.unparse(child.test) == 'response is not None and response.status_code == 403')
        latch = response_guard.body[0]
        require(len(response_guard.body) == 1 and isinstance(latch, ast.Try)
                and len(latch.body) == 1 and isinstance(latch.body[0], ast.Expr)
                and latch.body[0].value is block and send.lineno < block.lineno < settlement.lineno,
                'returned HTTP 403 must be durably blocked before attempting ledger settlement')
        require(not any(send.lineno < call.lineno < block.lineno for call in _calls(invoke, 'save')),
                'capture logging must not prevent blocking an already returned HTTP 403')
        require(len(latch.handlers) == 1 and ast.unparse(latch.handlers[0].type) == 'BaseException'
                and isinstance(latch.handlers[0].body[-1], ast.Raise)
                and not _calls(latch.handlers[0], 'self.ledger.finish'),
                'failed permission latch must retain its reservation and stop before settlement')
        require(len(settlements) == 1 and not any(isinstance(child, (ast.For, ast.While))
                    and _calls(child, 'self.ledger.finish') for child in _nodes(invoke)),
                'each physical invocation has one non-looping settlement call')
        settlement_owner = next(child for child in _nodes(invoke) if isinstance(child, ast.Try)
                                and any(settlement is item for statement in child.body for item in _nodes(statement)))
        require(len(settlement_owner.handlers) == 1
                and not _calls(settlement_owner.handlers[0], 'self.ledger.finish')
                and _contains(settlement_owner.handlers[0], 'preserve(error)') is not None
                and _contains(settlement_owner.handlers[0],
                    'if isinstance(error, CommitUnknown) or not isinstance(error, Exception):\n    raise') is not None
                and _contains(settlement_owner.handlers[0], 'raise converted from error') is not None,
                'settlement error facts and original CommitUnknown identity must remain attached')

        metered = _function(root, 'kb_platform/rag_testing/provider_capture.py', 'MeteredTransport._handle_admitted')
        received = _calls(metered, 'self.inner.handle_request')[0]
        block = _calls(metered, 'self.admission.block')[0]
        read = _calls(metered, 'response.read')[0]
        finishes = _calls(metered, 'self.ledger.finish') + _calls(metered, 'settle')
        require(received.lineno < block.lineno < read.lineno and all(block.lineno < call.lineno for call in finishes),
                'metered Luna/Google HTTP 403 latch must precede body reading and settlement')

        block = _function(root, 'kb_platform/provider_admission.py', 'ProviderAdmission.block')
        ephemeral = _contains(block, 'self._ephemeral.add(physical_id)')
        immutable = _calls(block, 'self._immutable')[0]
        require(ephemeral is not None and ephemeral.lineno < immutable.lineno,
                'failed event persistence must retain the local denial')
        snapshot = _function(root, 'kb_platform/provider_admission.py', 'ProviderAdmission.snapshot')
        require(_contains(snapshot, 'events, persisted, acknowledged, files = set(self._ephemeral), set(), set(), {}')
                is not None and _contains(snapshot, "persisted.add(row['physical_request_id'])") is not None,
                'snapshot must distinguish observed and durable event identities')
        require(_guard(snapshot, 'not ids.issubset(persisted)', 'ACKNOWLEDGEMENT_WITHOUT_PERMISSION_ERROR') is not None,
                'acknowledgements may refer only to validated durable events')
        checkpoint = ast.parse("digest({'files': files, 'observed_error_ids': sorted(events)})", mode='eval').body
        require(any(_shape(child) == _shape(checkpoint) for child in _nodes(snapshot)),
                'checkpoint must bind both immutable file content and all locally observed event identities')
        require(any(isinstance(child, ast.keyword) and child.arg == 'unpersisted_error_ids'
                    and ast.unparse(child.value) == 'sorted(events - persisted)' for child in _nodes(snapshot)),
                'private checkpoint snapshot must expose missing durable event identities')

        acknowledge = _function(root, 'kb_platform/provider_admission.py', 'ProviderAdmission.acknowledge')
        changed = _guard(acknowledge, "current['checkpoint'] != expected_checkpoint", 'PROVIDER_ADMISSION_CHECKPOINT_CHANGED')
        unknown = _guard(acknowledge, "current['unpersisted_error_ids']", 'PROVIDER_PERMISSION_RECEIPT_UNPERSISTED')
        append = _calls(acknowledge, 'self._immutable')[0]
        require(changed is not None and unknown is not None and changed.lineno < unknown.lineno < append.lineno,
                'ack append requires exact checkpoint and no unpersisted events')
        require(_contains(acknowledge, "current = self.snapshot()") is not None
                and any(isinstance(child, ast.Dict) and any(isinstance(key, ast.Constant)
                    and key.value == 'observed_error_ids' and ast.unparse(value) == "current['observed_error_ids']"
                    for key, value in zip(child.keys, child.values)) for child in _nodes(acknowledge)),
                'ack must capture the checked snapshot IDs, never clear a later event globally')
        for key in ('provider_probe_performed', 'budget_or_error_rows_modified'):
            require(any(isinstance(child, ast.Dict) and any(isinstance(field, ast.Constant)
                and field.value == key and isinstance(value, ast.Constant) and value.value is False
                for field, value in zip(child.keys, child.values)) for child in _nodes(acknowledge)),
                'ack receipt must explicitly preserve no-probe and no-budget/error-mutation facts')
        allowed = {'isinstance', 'evidence_reference.strip', 'ValueError', 'self.snapshot', 'uuid4', 'time.time', 'self._immutable'}
        require(all(ast.unparse(child.func) in allowed for child in _nodes(acknowledge) if isinstance(child, ast.Call)),
                'operator acknowledgement is append-only and cannot probe providers or mutate budgets/error rows')
        available = _function(root, 'kb_platform/provider_admission.py', 'ProviderAdmission.require_available')
        require(_contains(available, "blocked = self.snapshot()['state'] == 'BLOCKED'") is not None
                and _contains(available, 'exc.model_sent = False') is not None
                and any(isinstance(child, ast.ExceptHandler) and ast.unparse(child.type) == 'BaseException'
                        and any(isinstance(item, ast.Raise) for item in _nodes(child)) for child in _nodes(available)),
                'unreadable admission state must fail closed as known-unsent')
    except (OSError, ValueError, SyntaxError, StopIteration, IndexError, AttributeError) as error:
        errors.append('provider_admission: missing reviewed transition structure: ' + type(error).__name__)
    return errors
