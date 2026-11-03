import ast
from pathlib import Path


FLAGS = {
    'PlatformUpload': ('CheckIntegrity', 'ReserveFirst', 'ConsumeFirst', 'KeepConsumed', 'CheckCapacity'),
    'PlatformSnapshots': ('CheckBinding', 'CheckAuthorization', 'RecheckSnapshot', 'CloseStreams', 'CheckCapacity', 'CheckOffset'),
    'OCRUpload': ('CheckIntegrity', 'ConsumeFirst', 'KeepConsumed', 'ValidateResult', 'CheckCapacity'),
}
WITNESSES = {
    'PlatformUpload': {
        'TransferHeld': r'\E h \in Handles : phase[h] = "published" /\ h \in reservations /\ h \in effects',
        'Published': r'\E h \in Handles : phase[h] = "published" /\ h \in effects /\ calls[h] = 1',
        'CommittedFailure': r'\E h \in Handles : phase[h] = "failed" /\ h \in effects /\ upload[h] = "consumed"',
    },
    'PlatformSnapshots': {
        'ClosedBuild': r'closed /\ \E h \in Handles : h \in failedBuild /\ h \notin building /\ h \in built',
        'Completed': r'\E h \in Handles : position[h] = 2 /\ slot[h] = "disposed" /\ h \in delivered',
        'Revoked': r'revoked /\ \E h \in Handles : reading[h] = "rejected" /\ ~allowed[h]',
        'ClosedRead': r'closed /\ \E h \in Handles : reading[h] = "rejected" /\ allowed[h]',
    },
    'OCRUpload': {
        'Returned': r'\E h \in Handles : phase[h] = "returned" /\ calls[h] = 1 /\ validResult[h]',
        'Failed': r'\E h \in Handles : phase[h] = "failed" /\ calls[h] = 1 /\ upload[h] = "consumed"',
        'DrainedClose': r'closed /\ \E h \in Handles : phase[h] = "returned" /\ calls[h] = 1',
    },
}


def _method(root, path, owner, name):
    tree = ast.parse((root / path).read_text('utf-8'))
    return next(node for parent in tree.body if isinstance(parent, ast.ClassDef) and parent.name == owner
                for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _has(node, source):
    expected = ast.dump(ast.parse(source).body[0], include_attributes=False)
    return any(ast.dump(item, include_attributes=False) == expected for item in ast.walk(node))


def _claim_errors(root, path, owner, name, size_key, hash_key, reserve):
    node = _method(root, path, owner, name)
    errors = []
    locked = node.body[0] if node.body and isinstance(node.body[0], ast.With) else None
    if locked is None or ast.unparse(locked.items[0].context_expr) != 'self._lock':
        return [f'{owner}.{name}: claim requires the upload lock']
    expected = f"len(content) != expected['{size_key}'] or sha256(content).hexdigest() != expected['{hash_key}']"
    checks = [index for index, item in enumerate(locked.body) if isinstance(item, ast.If)
              and ast.unparse(item.test) == expected and any(isinstance(child, ast.Raise) for child in item.body)]
    deletes = [index for index, item in enumerate(locked.body)
               if isinstance(item, ast.Delete) and ast.unparse(item) == "del self._uploads[request['upload_id']]"]
    if not checks or not deletes or checks[0] >= deletes[0]:
        errors.append(f'{owner}.{name}: complete length and hash must precede consumption')
    if reserve:
        reservations = [index for index, item in enumerate(locked.body)
                        if _has(item, "identity = self._transfers.reserve(request['actor'], 'ingest')")]
        if not reservations or not checks or not deletes or not checks[0] < reservations[0] < deletes[0]:
            errors.append(f'{owner}.{name}: reserve result capacity before consuming the upload')
    provider = '_implementation.ingest' if reserve else 'ocr_provider.complete_json'
    provider_calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)
                      and isinstance(item.func, ast.Attribute) and ast.unparse(item.func) == 'self.' + provider]
    if len(provider_calls) != 1 or any(isinstance(item, ast.With) and
            any(ast.unparse(context.context_expr) == 'self._lock' for context in item.items)
            and provider_calls[0] in list(ast.walk(item)) for item in ast.walk(node)):
        errors.append(f'{owner}.{name}: provider must be invoked once outside the upload lock')
    if any(isinstance(item, (ast.Assign, ast.AnnAssign)) and 'self._uploads[' in ast.unparse(item).split('=')[0]
           for item in ast.walk(node)):
        errors.append(f'{owner}.{name}: consumed upload cannot be reinserted on failure')
    if not reserve and not _has(node, 'validate_ocr_response(result)'):
        errors.append(f'{owner}.{name}: validate provider result before returning it')
    return errors


def rpc_transfer_source_violations(root: Path) -> list[str]:
    paths = ('kb_platform/knowledge_platform_contract.py', 'kb_platform/platform_transfers.py',
             'kb_platform/knowledge_model_ports.py')
    if not all((root / path).is_file() for path in paths):
        return []
    errors = _claim_errors(root, paths[0], 'PlatformEndpoint', 'ingest_finish', 'size', 'sha256', True)
    errors.extend(_claim_errors(root, paths[2], 'AuxiliaryModelEndpoint', 'ocr', 'image_size', 'image_sha256', False))
    read = _method(root, paths[1], 'PlatformSnapshots', 'read')
    if not (len(read.body) == 3 and isinstance(read.body[0], ast.With)
            and isinstance(read.body[1], ast.Try) and isinstance(read.body[2], ast.With)
            and _has(read.body[1], 'authorize(snapshot.context)')):
        errors.append('PlatformSnapshots.read: authorization must run between lock sections')
    second = read.body[-1]
    guards = [item for item in getattr(second, 'body', []) if isinstance(item, ast.If)]
    if not guards or ast.unparse(guards[0].test) != 'self._get(actor, identity, kind) is not snapshot':
        errors.append('PlatformSnapshots.read: recheck the identical snapshot after authorization')
    if len(guards) < 2 or ast.unparse(guards[1].test) != 'type(offset) is not int or offset != snapshot.stream.tell()':
        errors.append('PlatformSnapshots.read: require the next exact integer offset')
    getter = _method(root, paths[1], 'PlatformSnapshots', '_get')
    if not any(isinstance(item, ast.If) and ast.unparse(item.test) ==
               'snapshot is None or snapshot.stream is None or snapshot.actor != actor or (snapshot.kind != kind)'
               for item in ast.walk(getter)):
        errors.append('PlatformSnapshots._get: bind stream, actor and kind')
    close = _method(root, paths[1], 'PlatformSnapshots', 'close')
    if not _has(close, 'self._closed = True') or not _has(close, 'self._discard(identity)'):
        errors.append('PlatformSnapshots.close: close admission and dispose owned streams')
    publish = _method(root, paths[1], 'PlatformSnapshots', 'publish')
    if not any(isinstance(item, ast.ExceptHandler) and ast.unparse(item.type) == 'BaseException'
               and _has(item, 'stream.close()') and _has(item, 'self._discard(identity)') for item in ast.walk(publish)):
        errors.append('PlatformSnapshots.publish: failed publication must release local stream and reservation')
    return errors


def rpc_transfer_owner_violations(root: Path, registry: dict) -> list[str]:
    errors = rpc_transfer_source_violations(root)
    controls = [row for machine in registry['machines'] for row in machine['negative_controls']]
    models = [row for machine in registry['machines'] for row in machine['models']]
    for name, flags in FLAGS.items():
        module = f'formal/rpc_transfer_owners/{name}.tla'
        if not (root / module).is_file():
            if any(row['module'] == module for row in models):
                errors.append('rpc transfer model is missing: ' + name)
            continue
        source = (root / module).read_text('utf-8')
        positive = [row for row in models if row['module'] == module]
        if len(positive) != 1:
            errors.append('rpc transfer requires exactly one registered positive: ' + name)
        for witness, predicate in WITNESSES[name].items():
            invariant = 'No' + witness + 'Witness'
            config = f'formal/rpc_transfer_owners/{name}{witness}Witness.cfg'
            if f'{invariant} == ~({predicate})' not in source:
                errors.append('rpc transfer requires terminal witness predicate: ' + invariant)
            if len([row for row in controls if row['module'] == module and row['config'] == config
                    and row.get('expected_invariant') == invariant]) != 1:
                errors.append('rpc transfer requires registered terminal witness: ' + invariant)
            path = root / config
            expected = 'SPECIFICATION Spec CONSTANTS ' + ' '.join(flag + ' = TRUE' for flag in flags)
            expected += ' INVARIANTS ' + invariant + ' CHECK_DEADLOCK FALSE'
            if not path.is_file() or ' '.join(path.read_text('utf-8').split()) != expected:
                errors.append('rpc transfer witness must use unmodified positive behavior: ' + invariant)
        for row in positive:
            text = ' '.join((root / row['config']).read_text('utf-8').split())
            expected = 'SPECIFICATION Spec CONSTANTS ' + ' '.join(flag + ' = TRUE' for flag in flags)
            expected += ' INVARIANTS ' + ' '.join(row['invariants']) + ' CHECK_DEADLOCK FALSE'
            if text != expected:
                errors.append('rpc transfer positive configuration enables a fault or constraint: ' + name)
    return errors
