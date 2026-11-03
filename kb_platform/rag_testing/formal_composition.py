import ast
import re


REVIEWED_EDGES = {
    'nested-request-ownership': (
        {'circuit_tool_retry', 'provider_circuit', 'tool_executor', 'provider_receipts', 'physical_budget'},
        'nested_request_ownership',
        {'PermitBalance', 'PhysicalNeedsPermit', 'NoUnknownReplay', 'UnknownRetainsCharge', 'TerminalNoWork'},
        {'kb_platform/rag_testing/formal_composition_contracts.py:FormalCompositionContracts.' + name for name in (
            'test_inner_retry_owns_two_physical_requests_under_one_circuit_permit',
            'test_outer_readonly_retry_acquires_a_new_permit_and_preserves_both_bills',
            'test_unknown_keeps_reserved_cost_and_is_not_retried_at_either_layer',
            'test_terminal_tool_reuse_never_reenters_circuit_receipts_or_ledger')}),
    'storage-cleanup-acknowledgement': (
        {'application_storage', 'redis_cache_provider', 'storage_registry'},
        'application_storage_shutdown',
        {'AcknowledgedCleanup', 'RetainedUntilClean', 'NoCleanupDuringOperation'},
        {'kb_platform/rag_testing/storage_shutdown_contracts.py:StorageShutdownContracts.'
         'test_application_retains_actual_redis_provider_after_failed_physical_close'}),
    'authority-session-commit': (
        {'knowledge_atomic_commit', 'session_commit', 'storage_transaction', 'authority_publication'},
        'knowledge_atomic_authority_commit',
        {'CurrentAuthorityAtCommit', 'AtomicCompletion', 'NoUnknownRetry'},
        {'kb_platform/rag_testing/knowledge_commit_contracts.py:KnowledgeCommitContracts.'
         'test_commit_holds_authority_until_physical_commit_against_an_independent_client'}),
}


def _shape(node):
    return ast.dump(node, include_attributes=False)


def _body(source):
    return [_shape(node) for node in ast.parse(source).body]


def nested_request_violations(root):
    path = root / 'kb_platform/remote.py'
    tree = ast.parse(path.read_text('utf-8'))
    decorators = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_provider_guard']
    guarded = [node for owner in decorators for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == 'guarded']
    errors = []
    if len(guarded) != 1:
        return ['composition: one provider permit owner is required']
    for owner in ('_Transport', 'GoogleEmbeddingProvider'):
        methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == owner
                   for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_post']
        if len(methods) != 1 or [ast.unparse(d) for d in methods[0].decorator_list] != ['_provider_guard']:
            errors.append(f'composition: {owner} physical retry callback must use the circuit permit owner')
    body = guarded[0].body
    acquired = (len(body) == 4 and isinstance(body[0], ast.Try)
                and [_shape(n) for n in body[0].body] == _body('permit = self._circuit.acquire()'))
    invocation = body[1] if len(body) > 1 else None
    settled = (isinstance(invocation, ast.Try) and not invocation.finalbody and not invocation.orelse
               and [_shape(n) for n in invocation.body] == _body('result = operation(self, *args, **kwargs)')
               and len(invocation.handlers) == 1
               and invocation.handlers[0].type is not None
               and ast.unparse(invocation.handlers[0].type) == 'BaseException')
    if not acquired or not settled:
        errors.append('composition: circuit admission must precede the single callback with BaseException settlement')
    if len(body) != 4 or [_shape(n) for n in body[-2:]] != _body(
            'self._circuit.finish(permit, success=True)\nreturn result'):
        errors.append('composition: successful circuit settlement must follow successful callback return')
    if settled:
        expected = _body("""
failure = classify_failure(exc)
admission = getattr(exc, 'code', '') in {
    'PROVIDER_RATE_LIMIT_WAIT_EXCEEDED', 'PROVIDER_RETRY_WAIT_EXCEEDED',
    'PROVIDER_DEADLINE_EXCEEDED', 'PROVIDER_REQUEST_LIMIT', 'PROVIDER_REQUEST_UNRESOLVED'}
self._circuit.finish(permit, failure=not admission and (failure.retryable or failure.outcome == 'UNKNOWN'
    or getattr(exc, 'code', '') == 'PROVIDER_TRANSPORT_ERROR'))
raise
""")
        if [_shape(n) for n in invocation.handlers[0].body] != expected:
            errors.append('composition: failed permit must settle once and preserve the original callback failure')
    allowed = {id(n) for n in ast.walk(guarded[0])}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {
                'self._circuit.acquire', 'self._circuit.finish'} and id(node) not in allowed:
            errors.append('composition: remote permit lifecycle cannot bypass its single owner')
    forbidden = {'_receipts', '_ledger', 'ledger', '_budget', 'budget'}
    if any(isinstance(n, ast.Attribute) and n.attr in forbidden for n in ast.walk(guarded[0])):
        errors.append('composition: circuit health cannot rewrite physical receipts or budget')
    return errors


def import_boundary_violations(root):
    tree = ast.parse((root / 'kb_platform/knowledge_transfer.py').read_text('utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'import_knowledge']
    if len(functions) != 1:
        return ['composition: legacy migration requires its domain import boundary']
    if any(isinstance(node, ast.Attribute) and node.attr in {'_package', '_metadata', '_space'}
           for node in ast.walk(functions[0])):
        return ['composition: domain import must not inspect target internals before administrator validation']
    return []


def composition_violations(root, registry):
    from scripts.authority_commit_checks import authority_commit_violations
    from .discovered_lifecycle_lint import discovered_lifecycle_violations
    from .storage_shutdown_lint import storage_shutdown_violations

    errors = []
    if not isinstance(registry, dict) or registry.get('schema_version') != 2:
        return ['composition: semantic edge registry requires schema version 2']
    machine_rows = registry.get('machines')
    if (not isinstance(machine_rows, list) or not machine_rows
            or any(not isinstance(m, dict) or not isinstance(m.get('id'), str)
                   or not isinstance(m.get('models'), list) or not isinstance(m.get('negative_controls'), list)
                   or not isinstance(m.get('status'), str) for m in machine_rows)):
        return ['composition: malformed machine inventory']
    machines = {m['id']: m for m in machine_rows}
    if len(machines) != len(machine_rows):
        errors.append('composition: duplicate machine identity')
    if any(m['status'] != 'partial' or not m['models'] or not m['negative_controls'] for m in machines.values()):
        errors.append('composition: every registered state owner requires positive and negative model evidence')
    discovery = registry.get('discovery')
    if not isinstance(discovery, dict) or discovery.get('roots') != ['kb_platform', 'rust', 'scripts']:
        errors.append('composition: discovery must include runtime code and executable scripts')

    def strings(value):
        return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)

    models = {}
    for machine in machines.values():
        for model in machine['models']:
            if not isinstance(model, dict) or not isinstance(model.get('id'), str) or not strings(model.get('invariants')):
                errors.append('composition: malformed model invariant declaration')
                continue
            if model['id'] in models:
                errors.append('composition: duplicate model identity')
            models[model['id']] = machine['id'], model
    edges = registry.get('composition_edges', [])
    if (not isinstance(edges, list) or any(not isinstance(e, dict) or not isinstance(e.get('id'), str) for e in edges)):
        return [*errors, 'composition: malformed semantic edge inventory']
    if {e['id'] for e in edges} != set(REVIEWED_EDGES) or len(edges) != len(REVIEWED_EDGES):
        errors.append('composition: required semantic handoffs cannot be missing, duplicated or self-declared')
    for edge in edges:
        expected = REVIEWED_EDGES.get(edge['id'])
        if (expected is None or set(edge) != {'id', 'participants', 'model', 'guarantees', 'assumptions', 'contracts'}
                or not all(strings(edge.get(key)) for key in ('participants', 'guarantees', 'assumptions', 'contracts'))
                or not isinstance(edge.get('model'), str)):
            errors.append(f"composition: malformed semantic handoff {edge['id']}")
            continue
        participants = edge.get('participants', [])
        owners, model_id, invariants, cases = expected
        if (len(set(participants)) != len(participants) or set(participants) - machines.keys()
                or set(participants) != owners):
            errors.append(f"composition: invalid participating owners for {edge.get('id')}")
        owned = models.get(edge.get('model'))
        if (edge['model'] != model_id or owned is None or owned[0] not in participants
                or not invariants <= set(edge['guarantees'])
                or set(edge.get('guarantees', [])) - set(owned[1]['invariants'])):
            errors.append(f"composition: guarantees must resolve to checked joint-model invariants for {edge.get('id')}")
        if not cases <= set(edge['contracts']):
            errors.append(f"composition: reviewed behavioral contracts missing for {edge['id']}")
        for case in edge.get('contracts', []):
            if case.count(':') != 1:
                errors.append('composition: evidence must name a discoverable contract test')
                continue
            path, symbol = case.split(':', 1)
            if (not re.fullmatch(r'kb_platform/rag_testing/[a-z0-9_]+_contracts\.py', path)
                    or not re.fullmatch(r'\w+\.test_\w+', symbol)):
                errors.append('composition: evidence must name a discoverable contract test')
                continue
            try:
                target = (root / path).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise ValueError('contract path escapes source root')
                tree = ast.parse(target.read_text('utf-8'))
            except (OSError, SyntaxError, ValueError) as error:
                errors.append(f'composition: unavailable contract {case}: {type(error).__name__}')
                continue
            cls, method = symbol.split('.')
            matches = [n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == cls
                       and any(ast.unparse(base) == 'unittest.TestCase' for base in c.bases)
                       for n in c.body if isinstance(n, ast.FunctionDef) and n.name == method]
            if len(matches) != 1:
                errors.append(f'composition: missing executable contract {case}')
    for name, check in (
        ('nested request', lambda: nested_request_violations(root)),
        ('import boundary', lambda: import_boundary_violations(root)),
        ('authority commit', lambda: authority_commit_violations(root, registry)),
        ('discovered lifecycle', lambda: discovered_lifecycle_violations(root)),
        ('storage shutdown', lambda: storage_shutdown_violations(
            (root / 'kb_platform/storage_adapters/redis_cache.py').read_text('utf-8'),
            (root / 'kb_platform/application_storage.py').read_text('utf-8'))),
    ):
        try:
            errors.extend(check())
        except (OSError, SyntaxError, ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            errors.append(f'composition: {name} enforcement failed: {type(error).__name__}')
    return errors
