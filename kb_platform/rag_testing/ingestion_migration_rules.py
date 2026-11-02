import ast
from pathlib import Path


def _shape(node):
    return ast.dump(node, include_attributes=False)


def _statement(source):
    return _shape(ast.parse(source).body[0])


def _expression(source):
    return _shape(ast.parse(source, mode='eval').body)


def _contains(node, statement):
    expected = _statement(statement)
    return any(_shape(child) == expected for child in ast.walk(node))


def _function(root, relative, qualified):
    node = ast.parse((Path(root) / relative).read_text('utf-8'))
    for name in qualified.split('.'):
        node = next(child for child in node.body if isinstance(child, (ast.ClassDef, ast.FunctionDef))
                    and child.name == name)
    return node


def _guard(node, condition, code):
    expected = _expression(condition)
    return next((child for child in ast.walk(node) if isinstance(child, ast.If)
                 and _shape(child.test) == expected and any(
                     isinstance(raised, ast.Raise) and any(isinstance(value, ast.Constant)
                         and value.value == code for value in ast.walk(raised))
                     for item in child.body for raised in ast.walk(item))), None)


def ingestion_migration_violations(root):
    errors = []

    def require(value, message):
        if not value:
            errors.append('ingestion_migration: ' + message)

    try:
        missing = _function(root, 'kb_platform/documents.py', '_Bundle.missing')
        require(_contains(missing, "self.result['status'] = 'PARTIAL'"), 'missing capability must make parse PARTIAL')
        native = _function(root, 'kb_platform/documents.py', 'parse_native_document')
        failed = next(child for child in ast.walk(native) if isinstance(child, ast.ExceptHandler))
        require(_contains(failed, "bundle.result['status'] = 'FAILED'") and all(
            _contains(failed, f"bundle.result['{collection}'] = []")
            for collection in ('elements', 'evidence_spans', 'anchors')), 'parse failure must clear unusable evidence')
        stage = _function(root, 'kb_platform/core.py', 'Platform.stage')
        readiness = _guard(stage, "any(row['document']['status'] != 'READY' for row in rows)", 'INCOMPLETE_PARSE')
        insert = next(child for child in ast.walk(stage) if isinstance(child, ast.Call)
                      and ast.unparse(child.func) == 'conn.execute' and child.args
                      and ast.unparse(child.args[0]) == 'KnowledgeQuery.BUILD_INSERT')
        require(readiness is not None and readiness.lineno < insert.lineno, 'only READY parses may precede BUILD_INSERT')
        evidence = _guard(stage, 'not units', 'NO_EVIDENCE')
        require(evidence is not None and evidence.lineno < insert.lineno, 'build admission must also require nonempty evidence')
        ingest = _function(root, 'kb_platform/core.py', 'Platform.ingest')
        integrity = _guard(ingest, "document.get('original_sha256') != digest or document.get('source_version_id') != digest",
                           'SOURCE_INTEGRITY')
        stale = _guard(ingest, "current['revision'] != existing['revision']", 'STALE_PARSE')
        commits = [child for child in ast.walk(ingest) if isinstance(child, ast.With)
                   and any(_contains(child, expression) for expression in (
                       'self._lock_authority(conn, actor)', "self._auth(conn, actor, kb_id, 'kb.write')"))]
        commit = next(child for child in commits if _contains(child, 'self._lock_authority(conn, actor)'))
        require(integrity is not None and integrity.lineno < commit.lineno,
                'original hash/version must be checked before committing ingestion metadata')
        updates = [child for child in ast.walk(commit) if isinstance(child, ast.Call)
                   and ast.unparse(child.func) == 'conn.execute' and child.args
                   and ast.unparse(child.args[0]) == 'KnowledgeQuery.SOURCE_UPDATE']
        require(stale is not None and len(updates) == 1 and stale.lineno < updates[0].lineno
                and _contains(commit, "self._auth(conn, actor, kb_id, 'kb.write')"),
                'ingestion must reauthorize and reject changed source revisions before update')

        transfer = _function(root, 'kb_platform/knowledge_transfer.py', 'import_knowledge')
        require(_guard(transfer, 'target_offline is not True', 'TRANSFER_TARGET_MUST_BE_OFFLINE') is not None
                and _guard(transfer, "expected_identity.dataset_id == package['source']['dataset_id']",
                           'KNOWLEDGE_TRANSFER_REQUIRES_NEW_DATASET') is not None,
                'knowledge transfer requires explicit offline and distinct dataset identity')
        require(_contains(transfer, "transformed = deepcopy(package['tables'])")
                and _contains(transfer, "row['manifest_id'], row['revision'] = None, row['revision'] + 1")
                and _contains(transfer, "row['status'] = 'STAGING'"),
                'transfer must copy records and reset publication before import')
        resets = [_statement("row['manifest_id'], row['revision'] = None, row['revision'] + 1"),
                  _statement("row['status'] = 'STAGING'")]
        import_call = next(child for child in ast.walk(transfer) if isinstance(child, ast.Call)
                           and ast.unparse(child.func) == 'import_structured_snapshot')
        require(all(any(_shape(child) == expected and child.lineno < import_call.lineno
                        for child in ast.walk(transfer)) for expected in resets),
                'publication reset assignments must precede the import transaction')
        structured = _function(root, 'kb_platform/storage_adapters/structured_transfer.py', 'import_structured_snapshot')
        identity = _function(root, 'kb_platform/storage_adapters/structured_transfer.py', '_identity')
        require(_guard(identity, "len(rows) != 1 or rows[0]['identity_key'] != 1 or "
                       "rows[0]['space_key'] != expected.space.key or rows[0]['dataset_id'] != expected.dataset_id",
                       'TRANSFER_DATASET_IDENTITY_MISMATCH') is not None,
                'structured transfer must validate both immutable space and dataset identities')
        transactions = [child for child in structured.body if isinstance(child, ast.With)
                        and ast.unparse(child.items[0].context_expr) == 'migration_connection(target._space)']
        receipt = 'connection.execute(_RECEIPTS.insert().values(source_sha256=source_sha256, receipt_json=serialized))'
        data = 'connection.execute(table.insert().values(row))'
        require(len(transactions) == 2 and _contains(transactions[1], data)
                and _contains(transactions[1], receipt) and _contains(transactions[1], '_identity(connection, expected_identity)'),
                'domain data and receipt must share the identity-checked transaction')
        require(_guard(structured, 'previous is not None and previous != serialized', 'TRANSFER_RECEIPT_CONFLICT') is not None
                and _guard(structured, 'content_hash(actual) != content_hash(expected)', 'TRANSFER_TARGET_CHANGED') is not None
                and _guard(structured, 'any(actual.values())', 'TRANSFER_TARGET_NOT_EMPTY') is not None,
                'repeated imports must verify exact content and never overwrite a nonempty target')

        plan = _function(root, 'kb_platform/storage_reset.py', 'plan_business_reset')
        require(_contains(plan, 'selected = deepcopy(configuration)')
                and _contains(plan, 'consumers = tuple(sorted(name for name, role in roles.items() if role in BUSINESS_ROLES))')
                and _contains(plan, "del binding['dataset_id']"), 'reset must select only business roles and allocate fresh identities')
        validation = _function(root, 'kb_platform/storage_reset.py', '_validate')
        require(_guard(validation, "any(DEFAULT_ROLES.get(name) == 'ledger' and role != 'ledger' for name, role in roles.items())",
                       'RESET_LEDGER_ROLE_IMMUTABLE') is not None, 'ledger roles cannot become resettable business roles')
        initialize = _function(root, 'kb_platform/storage_reset.py', 'initialize_business_reset')
        require(_contains(initialize, "business = {name: selected['bindings'][name] for name in plan.consumers}")
                and _contains(initialize, "selected['bindings'].update(initialized['bindings'])"),
                'reset initialization must be restricted to planned consumers')
        reset = _function(root, 'kb_platform/storage_reset.py', 'reset_to_file')
        calls = [(ast.unparse(child.func), child.lineno) for child in ast.walk(reset) if isinstance(child, ast.Call)]
        sequence = ['initialize_business_reset', 'json.dump', 'stream.flush', 'os.fsync', 'os.link']
        positions = [[line for name, line in calls if name == expected] for expected in sequence]
        require(all(len(found) == 1 for found in positions)
                and [found[0] for found in positions] == sorted(found[0] for found in positions)
                and not any(name in {'os.replace', 'os.rename', 'output.write_text', 'output.write_bytes'} for name, _ in calls),
                'reset must initialize, serialize and fsync before exclusive publication; overwrite is forbidden')
    except (OSError, SyntaxError, KeyError, StopIteration, IndexError, ValueError, AttributeError) as error:
        errors.append('ingestion_migration: invalid transition structure: ' + type(error).__name__)
    return errors
