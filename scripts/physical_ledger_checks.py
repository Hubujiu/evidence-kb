import ast


EXPECTED = {
    '_sum_rows': '''
        total = dict.fromkeys(caps, 0)
        for row in rows:
            reserved, usage = ((row['reserved'], row['usage']) if hasattr(row, 'keys') else row)
            reserve = json.loads(reserved)
            actual = json.loads(usage) if usage else {}
            for resource in total:
                total[resource] += actual.get(resource, reserve[resource])
        return total
    ''',
    'reserve': '''
        with self.admission():
            return self._reserve(request_id, resources, category, require_settled=require_settled)
    ''',
    '_reserve.transaction': '''
        if require_settled and db.execute(
                select(budget_calls.c.id).where(budget_calls.c.status.in_(('STARTED', 'UNKNOWN')))
                .limit(1)).fetchone():
            raise ValueError('UNRESOLVED_PHYSICAL_REQUEST')
        if db.execute(select(budget_calls.c.id).where(budget_calls.c.id == request_id)).fetchone():
            raise ValueError('physical request already registered; inspect its result, never resend')
        used = self._totals(db, self.caps)
        if any(used[k] + resources[k] > cap for k, cap in self.caps.items()):
            raise ValueError('BUDGET_INSUFFICIENT')
        db.insert(budget_calls, {'id': request_id, 'status': 'STARTED', 'reserved': json.dumps(resources),
            'usage': None, 'category': category, 'diagnostic': None})
    ''',
    'finish.transaction': '''
        cursor = db.execute(budget_calls.update().where(budget_calls.c.id == request_id,
            budget_calls.c.status == 'STARTED').values(status=status,
            usage=json.dumps(usage) if usage is not None else None, diagnostic=diagnostic))
        if cursor.rowcount != 1:
            raise ValueError('missing or terminal physical request')
    ''',
    'summary': '''
        rows = self._records()
        normalized = [(row['status'], row['reserved'], row['usage']) for row in rows]
        totals = self._sum_rows([row[1:] for row in normalized], self.caps)
        return {'physical_requests': len(rows), 'effective_usage_or_reserved': totals,
                'unknown_usage_requests': sum(u is None or set(json.loads(u)) != set(self.caps) for _, _, u in normalized),
                'pending_or_unknown': sum(s in ('STARTED', 'UNKNOWN') for s, _, _ in normalized),
                'over_budget': any(totals[k] > cap for k, cap in self.caps.items())}
    ''',
}


def _shape(value):
    if isinstance(value, ast.AST):
        return type(value).__name__, tuple((key, _shape(item)) for key, item in ast.iter_fields(value)
                                          if key != 'ctx')
    if isinstance(value, list):
        return tuple(_shape(item) for item in value)
    return value


def ledger_core_violations(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ['physical ledger core: source must parse']
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'BudgetLedger']
    if len(classes) != 1:
        return ['physical ledger core: single BudgetLedger owner required']
    functions = {node.name: node for node in classes[0].body if isinstance(node, ast.FunctionDef)}
    errors = []
    for symbol, expected in EXPECTED.items():
        name, _, part = symbol.partition('.')
        owner = functions.get(name)
        actual = None if owner is None else owner.body
        if part == 'transaction' and owner is not None:
            transactions = [node for node in owner.body if isinstance(node, ast.With)
                            and len(node.items) == 1 and _shape(node.items[0].context_expr) ==
                            _shape(ast.parse('self._db()', mode='eval').body)]
            actual = transactions[0].body if len(transactions) == 1 else None
        required = ast.parse('if True:\n' + expected).body[0].body
        if _shape(actual) != _shape(required):
            errors.append('physical ledger core: ' + symbol + ' must retain reviewed accounting/CAS policy')
    reserve = functions.get('reserve')
    if (reserve is None or len(reserve.args.kwonlyargs) != 1
            or reserve.args.kwonlyargs[0].arg != 'require_settled'
            or _shape(reserve.args.kw_defaults) != _shape([ast.Constant(value=False)])):
        errors.append('physical ledger core: require_settled must remain an explicit default-false policy')
    return errors


def local_ranker_settlement_violations(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ['local ranker settlement: source must parse']
    ranks = [node for group in tree.body if isinstance(group, ast.ClassDef) and group.name == 'LocalRanker'
             for node in group.body if isinstance(node, ast.FunctionDef) and node.name == 'rank']
    if len(ranks) != 1:
        return ['local ranker settlement: rank owner missing']
    call_shape = _shape(ast.parse('self.ledger.finish', mode='eval').body)
    def calls(nodes):
        return [node for outer in nodes for node in ast.walk(outer)
                if isinstance(node, ast.Call) and _shape(node.func) == call_shape]

    attempts = [node for node in ast.walk(ranks[0]) if isinstance(node, ast.Try) and calls(node.orelse)]
    if len(attempts) != 1:
        return ['local ranker settlement: success finish must be outside the inference error handler']
    attempt = attempts[0]
    expected_success = ast.parse("self.ledger.finish(physical_id, status='COMPLETE', usage=resources)").body
    expected_error = ast.parse("self.ledger.finish(physical_id, status='ERROR', diagnostic=str(exc))\nraise").body
    valid = (len(calls([ranks[0]])) == 2 and not calls(attempt.body) and not attempt.finalbody
             and _shape(attempt.orelse) == _shape(expected_success) and len(attempt.handlers) == 1)
    if valid:
        handler = attempt.handlers[0]
        valid = (handler.name == 'exc' and _shape(handler.type) == _shape(ast.Name(id='Exception'))
                 and _shape(handler.body) == _shape(expected_error))
    return [] if valid else ['local ranker settlement: preserve one finish per outcome and bare rethrow']
