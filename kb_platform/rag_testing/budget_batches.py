from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .budget import BudgetLedger
from .contracts import digest

PROTOCOL = 'linked-budget-predecessor/1'


def _snapshot(path):
    path = Path(path).resolve()
    before = sha256(path.read_bytes()).hexdigest()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db, db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        caps = json.loads(db.execute('SELECT caps FROM config WHERE id=1').fetchone()[0])
        if not caps or any(type(value) is not int or value < 0 for value in caps.values()):
            raise ValueError('INVALID_PREDECESSOR_CAPS')
        rows = [{'id': identity, 'status': status, 'reserved': json.loads(reserved),
                 'usage': json.loads(usage) if usage is not None else None,
                 'category': category, 'diagnostic': diagnostic}
                for identity, status, reserved, usage, category, diagnostic in db.execute(
                    'SELECT id,status,reserved,usage,category,diagnostic FROM calls ORDER BY id')]
        totals = BudgetLedger._totals(db, caps)
    if sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError('LEDGER_CHANGED_DURING_SNAPSHOT')
    for row in rows:
        if row['status'] not in ('COMPLETE', 'ERROR', 'UNKNOWN', 'STARTED'):
            raise ValueError('INVALID_PREDECESSOR_REQUEST_STATE')
        for resources, complete in ((row['reserved'], True), (row['usage'], False)):
            if resources is None:
                continue
            if (not set(resources) <= set(caps) or complete and set(resources) != set(caps) or
                    any(type(value) is not int or value < 0 for value in resources.values())):
                raise ValueError('INVALID_PREDECESSOR_RESOURCE_DIMENSIONS')
    summary = {'physical_requests': len(rows), 'effective_usage_or_reserved': totals,
        'unknown_usage_requests': sum(row['usage'] is None or set(row['usage']) != set(caps) for row in rows),
        'pending_or_unknown': sum(row['status'] in ('STARTED', 'UNKNOWN') for row in rows),
        'over_budget': any(totals[key] > caps[key] for key in caps)}
    return {'protocol': PROTOCOL, 'path': str(path), 'database_sha256': before,
            'caps': caps, 'rows': rows, 'rows_sha256': digest(rows),
            'snapshot_sha256': digest({'caps': caps, 'rows': rows}), 'summary': summary,
            'read_protocol': 'SQLite URI mode=ro; query_only; one read transaction; logical rows hash',
            'physical_file_hash_is_not_a_WAL_snapshot': True}


def freeze_predecessor(path, expected_file_sha256=None):

    value = _snapshot(path)
    if expected_file_sha256 is not None and value['database_sha256'] != expected_file_sha256.lower():
        raise ValueError('PREDECESSOR_FILE_SHA256_MISMATCH')
    if value['summary']['pending_or_unknown']:
        raise ValueError('UNRESOLVED_PREDECESSOR_LEDGER')
    if value['summary']['over_budget']:
        raise ValueError('PREDECESSOR_ALREADY_OVER_BUDGET')
    return value


def _validate_frozen_identity(frozen):
    if (not isinstance(frozen, dict) or frozen.get('protocol') != PROTOCOL or
            frozen.get('rows_sha256') != digest(frozen.get('rows')) or
            frozen.get('snapshot_sha256') != digest({'caps': frozen.get('caps'), 'rows': frozen.get('rows')})):
        raise ValueError('INVALID_FROZEN_PREDECESSOR')


def _validate_frozen(frozen):
    _validate_frozen_identity(frozen)
    current = _snapshot(frozen['path'])
    if (current['snapshot_sha256'] != frozen['snapshot_sha256'] or
            current['database_sha256'] != frozen['database_sha256']):
        raise ValueError('PREDECESSOR_LEDGER_CHANGED')
    if current['summary']['pending_or_unknown']:
        raise ValueError('UNRESOLVED_PREDECESSOR_LEDGER')
    if current['summary']['over_budget']:
        raise ValueError('PREDECESSOR_ALREADY_OVER_BUDGET')
    return current


def _unique(rows_by_ledger):
    owners, duplicates = {}, set()
    for path, rows in rows_by_ledger:
        for row in rows:
            if row['id'] in owners:
                duplicates.add(row['id'])
            owners[row['id']] = path
    return sorted(duplicates)


class LinkedBudgetLedger:


    def __init__(self, path, caps, *, predecessors):
        self.path, self.caps = Path(path).resolve(), deepcopy(caps)
        self.predecessors = deepcopy(list(predecessors))
        if not self.predecessors:
            raise ValueError('EXPLICIT_PREDECESSOR_REQUIRED')
        paths = [str(Path(value['path']).resolve()) for value in self.predecessors]
        if len(set(paths)) != len(paths) or str(self.path) in paths:
            raise ValueError('DUPLICATE_OR_SELF_PREDECESSOR')
        parents = self._parents()
        if any(set(parent['caps']) != set(self.caps) for parent in parents):
            raise ValueError('BATCH_RESOURCE_DIMENSIONS_DIFFER')
        if _unique([(parent['path'], parent['rows']) for parent in parents]):
            raise ValueError('DUPLICATE_PHYSICAL_REQUEST_ID')
        self.ledger = BudgetLedger(self.path, self.caps)
        child = _snapshot(self.path)
        if _unique([*((p['path'], p['rows']) for p in parents), (str(self.path), child['rows'])]):
            raise ValueError('DUPLICATE_PHYSICAL_REQUEST_ID')
        if child['summary']['pending_or_unknown']:
            raise ValueError('UNRESOLVED_CHILD_LEDGER')

    def _parents(self):
        return [_validate_frozen(value) for value in self.predecessors]

    def reserve(self, request_id, resources, category='runtime'):
        parents = self._parents()
        if any(request_id == row['id'] for parent in parents for row in parent['rows']):
            raise ValueError('DUPLICATE_PHYSICAL_REQUEST_ID')
        return self.ledger.reserve(request_id, resources, category=category, require_settled=True)

    def finish(self, request_id, *, status, usage=None, diagnostic=None):

        return self.ledger.finish(request_id, status=status, usage=usage, diagnostic=diagnostic)

    def summary(self):
        self._parents()
        return self.ledger.summary()

    def cumulative_summary(self):
        return cumulative_summary(self.predecessors, self.path)


def cumulative_summary(predecessors, child_path):

    predecessors = list(predecessors)
    for predecessor in predecessors:
        _validate_frozen_identity(predecessor)
    parents = [_snapshot(value['path']) for value in predecessors]
    child = _snapshot(child_path)
    all_ledgers = [*parents, child]
    if any(set(value['caps']) != set(child['caps']) for value in parents):
        raise ValueError('BATCH_RESOURCE_DIMENSIONS_DIFFER')
    duplicates = _unique([(value['path'], value['rows']) for value in all_ledgers])
    changed = [old['path'] for old, current in zip(predecessors, parents, strict=True)
               if old['snapshot_sha256'] != current['snapshot_sha256'] or
               old['database_sha256'] != current['database_sha256']]
    totals = {key: sum(value['summary']['effective_usage_or_reserved'][key] for value in all_ledgers)
              for key in child['caps']} if not duplicates else None
    pending = sum(value['summary']['pending_or_unknown'] for value in all_ledgers)
    over_budget = any(value['summary']['over_budget'] for value in all_ledgers)
    return {'protocol': 'linked-budget-cumulative-summary/1',
        'status': 'BLOCKED' if changed or duplicates or pending or over_budget else 'SETTLED',
        'predecessors': [{'path': value['path'], 'caps': value['caps'], 'summary': value['summary'],
                          'snapshot_sha256': value['snapshot_sha256']} for value in parents],
        'new_batch': {'path': child['path'], 'caps': child['caps'], 'summary': child['summary'],
                      'snapshot_sha256': child['snapshot_sha256']},
        'cumulative': {'physical_requests': None if duplicates else
                          sum(value['summary']['physical_requests'] for value in all_ledgers),
            'registered_rows_including_duplicates': sum(value['summary']['physical_requests'] for value in all_ledgers),
            'effective_usage_or_reserved': totals, 'pending_or_unknown': pending,
            'over_budget': over_budget,
            'unknown_usage_requests': sum(value['summary']['unknown_usage_requests'] for value in all_ledgers),
            'monetary_cost': 'NOT_MEASURED'},
        'predecessor_drift': changed, 'duplicate_physical_request_ids': duplicates,
        'caps_are_independent_not_a_merged_spendable_pool': True,
        'admission_scope': 'Child requests only. Predecessor producers must remain quiescent; '
                           'an unmodified running server has no shared cross-process batch lock.',
        'new_remote_calls_from_reporting': 0}
