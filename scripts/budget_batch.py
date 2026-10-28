"""Freeze/link/audit finite supplementary budgets; never send a model request."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.rag_testing.budget_batches import (LinkedBudgetLedger, cumulative_summary,  # noqa: E402
                                                    freeze_predecessor)


def _read(path):
    return json.loads(Path(path).read_text('utf-8'))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    freeze = sub.add_parser('freeze')
    freeze.add_argument('--prior-ledger', type=Path, required=True)
    freeze.add_argument('--expected-sha256')
    freeze.add_argument('--output', type=Path, required=True)
    create = sub.add_parser('create')
    create.add_argument('--predecessor', type=Path, action='append', required=True)
    create.add_argument('--caps', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    report = sub.add_parser('report')
    report.add_argument('--predecessor', type=Path, action='append', required=True)
    report.add_argument('--ledger', type=Path, required=True)
    report.add_argument('--output', type=Path, required=True)
    runtime = sub.add_parser('register-runtime', help='Register a finite runtime manifest without creating/spending its ledger')
    runtime.add_argument('--predecessor', type=Path, action='append', required=True)
    runtime.add_argument('--delivery-root', type=Path, required=True)
    runtime.add_argument('--tenant', required=True)
    runtime.add_argument('--semantic-predecessor', type=Path, required=True)
    runtime.add_argument('--id', required=True)
    runtime.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Use a new output path; existing artifacts stay read-only')
    if args.command == 'freeze':
        value = freeze_predecessor(args.prior_ledger, args.expected_sha256)
        _write(args.output, value)
    elif args.command == 'create':
        predecessors = [_read(path) for path in args.predecessor]
        caps = _read(args.caps)
        ledger = LinkedBudgetLedger(args.output / 'physical-calls.sqlite3', caps, predecessors=predecessors)
        _write(args.output / 'batch.json', {'protocol': 'finite-budget-batch/1', 'caps': caps,
            'predecessors': predecessors, 'ledger': str(ledger.path), 'new_remote_calls': 0})
        _write(args.output / 'cumulative-summary.json', ledger.cumulative_summary())
    elif args.command == 'report':
        value = cumulative_summary([_read(path) for path in args.predecessor], args.ledger)
        _write(args.output, value)
    else:
        import re
        from kb_platform.delivery import RUNTIME_CAPS

        predecessors = [_read(path) for path in args.predecessor]
        parents = [freeze_predecessor(value['path'], value['database_sha256']) for value in predecessors]
        if any(parent['snapshot_sha256'] != old['snapshot_sha256']
               for old, parent in zip(predecessors, parents, strict=True)):
            raise ValueError('PREDECESSOR_LEDGER_CHANGED')
        paths = {parent['path'] for parent in parents}
        original = str(args.delivery_root.resolve() / 'physical-calls.sqlite3')
        semantic = str(args.semantic_predecessor.resolve())
        if (not re.fullmatch(r'runtime-[a-zA-Z0-9-]{1,80}', args.id) or not args.tenant
                or len(paths) != len(parents) or original not in paths or semantic not in paths
                or original == semantic or args.output.resolve() == args.delivery_root.resolve()):
            raise ValueError('EXPLICIT_DISTINCT_ORIGINAL_AND_SEMANTIC_PREDECESSORS_REQUIRED')
        _write(args.output / 'batch.json', {'protocol': 'delivery-runtime-budget-batch/1', 'id': args.id,
            'tenant_id': args.tenant, 'caps': RUNTIME_CAPS, 'semantic_predecessor': semantic,
            'predecessors': predecessors, 'registered_only': True,
            'new_model_calls': 0, 'new_embedding_calls': 0})
    print(json.dumps({'command': args.command, 'output': str(args.output.resolve()), 'new_remote_calls': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
