import ast


def _same(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(ast.parse(right).body[0], include_attributes=False)


def receipt_database_violations(source):
    tree = ast.parse(source)
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ReceiptDatabase']
    if len(owners) != 1:
        return ['receipt database owner missing or ambiguous']
    methods = {node.name: node for node in owners[0].body if isinstance(node, ast.FunctionDef)}
    errors = []
    locked = methods.get('_locked')
    blocks = [] if locked is None else [node for node in locked.body if isinstance(node, ast.With)]
    if (len(blocks) != 1 or len(blocks[0].items) != 1
            or ast.unparse(blocks[0].items[0].context_expr) != 'self.database.transaction(write=True)'
            or not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == 'with_for_update' for node in ast.walk(blocks[0]))
            or not any(isinstance(node, ast.Yield) for node in ast.walk(blocks[0]))):
        errors.append('receipt database requires write transaction and locked logical read before yield')
    expected = {
        '_locked': ["logical_hash = sha256(logical_id.encode('utf-8')).hexdigest()",
                    "with self.database.transaction(write=True) as db:\n"
                    "    if create:\n        db.insert(receipt_logical, {'logical_hash': logical_hash, 'row_json': '{}'},\n"
                    "            conflict_keys=('logical_hash',))\n"
                    "    row = db.execute(select(receipt_logical.c.row_json).where(\n"
                    "        receipt_logical.c.logical_hash == logical_hash).with_for_update()).fetchone()\n"
                    "    yield db, logical_hash, None if row is None else json.loads(row['row_json'])"],
        'begin': ["with self._locked(logical_id, create=True) as (db, logical_hash, previous):\n"
                  "    if previous:\n        validate(previous)\n"
                  "    self._save(db, logical_hash, row, create=True)", "return row['physical_request_id']"],
        'transition': ["with self._locked(logical_id) as (db, logical_hash, active):\n"
                       "    row = db.execute(select(receipt_physical.c.row_json).where(\n"
                       "        receipt_physical.c.physical_id == physical_id)).fetchone()\n"
                       "    value = None if row is None else json.loads(row['row_json'])\n"
                       "    mutate(value, active)\n    self._save(db, logical_hash, value)"],
        '_save': ["physical, serialized = row['physical_request_id'], json.dumps(row, ensure_ascii=False)",
                  "if create:\n    db.insert(receipt_physical, {'physical_id': physical, 'row_json': serialized})\n"
                  "else:\n    db.execute(receipt_physical.update().where(receipt_physical.c.physical_id == physical)\n"
                  "        .values(row_json=serialized))",
                  "db.execute(receipt_logical.update().where(receipt_logical.c.logical_hash == logical_hash)\n"
                  "    .values(row_json=serialized))"],
    }
    for name, statements in expected.items():
        method = methods.get(name)
        if (method is None or len(method.body) != len(statements)
                or not all(_same(node, expected) for node, expected in zip(method.body, statements, strict=True))):
            errors.append(name + ': receipt pair must share transaction and return identity only after commit acknowledgement')
    return errors


def receipt_send_order_violations(source):
    tree = ast.parse(source)
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == '_Transport']
    methods = [node for owner in owners for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == '_send_attempt']
    begins = [node for method in methods for node in ast.walk(method) if isinstance(node, ast.Call)
              and ast.unparse(node.func) == 'self._receipts.begin']
    sends = [node for method in methods for node in ast.walk(method) if isinstance(node, ast.Call)
             and ast.unparse(node.func) == 'self._request_response']
    issued = [node for method in methods for node in ast.walk(method) if isinstance(node, ast.AugAssign)
              and ast.unparse(node.target) == 'self._issued_requests']
    if len(methods) == len(begins) == len(sends) == len(issued) == 1 and begins[0].lineno < issued[0].lineno < sends[0].lineno:
        return []
    return ['receipt begin acknowledgement must precede issued count and HTTP dispatch']


def receipt_database_transition_violations(source):
    tree = ast.parse(source)
    owners = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RequestReceipts']
    if len(owners) != 1:
        return ['receipt database transition owner missing or ambiguous']
    methods = {node.name: node for node in owners[0].body if isinstance(node, ast.FunctionDef)}
    errors = []
    for name, states, code in (('finish', "row['status'] != 'STARTED'", 'INVALID_PHYSICAL_REQUEST_TRANSITION'),
                               ('reconcile', "row['status'] not in {'STARTED', 'UNKNOWN'}", 'INVALID_REQUEST_RECONCILIATION')):
        method = methods.get(name)
        mutators = [] if method is None else [node for node in ast.walk(method)
                                             if isinstance(node, ast.FunctionDef) and node.name == 'mutate']
        guard = ("if row is None or active is None or row['logical_request_id'] != logical_id or " + states
                 + " or active['physical_request_id'] != physical_id:\n    raise RuntimePolicyError(" + repr(code) + ")")
        if len(mutators) != 1 or not mutators[0].body or not _same(mutators[0].body[0], guard):
            errors.append(name + ': receipt database CAS must reject wrong logical, stale physical and illegal predecessor')
    previous = methods.get('_validate_previous')
    guard = "if previous['status'] in {'STARTED', 'UNKNOWN'}:\n    raise RuntimePolicyError('PROVIDER_REQUEST_UNRESOLVED')"
    if previous is None or not previous.body or not _same(previous.body[0], guard):
        errors.append('receipt database STARTED and UNKNOWN must block a replacement begin')
    reconcile = methods.get('reconcile')
    guard = ("if outcome not in {'COMPLETE', 'ERROR'} or not isinstance(evidence_reference, str) or "
             "not evidence_reference.strip():\n    raise ValueError('RECONCILIATION_EVIDENCE_REQUIRED')")
    if reconcile is None or not reconcile.body or not _same(reconcile.body[0], guard):
        errors.append('receipt database reconciliation requires explicit terminal outcome evidence')
    return errors
