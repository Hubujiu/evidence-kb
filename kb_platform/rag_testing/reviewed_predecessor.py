"""Link a new budget batch to one explicitly reviewed unresolved predecessor.

The old UNKNOWN remains UNKNOWN and its full reservation remains in reports.
No old request is retried. All predecessor rows and the review are rechecked
before every admission; child UNKNOWN/STARTED still blocks further requests.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from .budget_batches import LinkedBudgetLedger, _snapshot, _validate_frozen, _validate_frozen_identity


def freeze_reviewed_predecessor(path, review_path):
    path, review_path = Path(path).resolve(strict=True), Path(review_path).resolve(strict=True)
    review_hash = sha256(review_path.read_bytes()).hexdigest()
    review = json.loads(review_path.read_text('utf-8'))
    if review.get('schema') == 'competitive-model-outcomes-review/1':
        snapshot = _snapshot(path)
        if (review.get('operator') != 'codex-local-audit' or not review.get('reason') or
                review.get('old_unknown_remains_unknown_and_reserved') is not True or
                review.get('snapshot') != snapshot or snapshot['summary']['over_budget'] or
                any(row['status'] == 'STARTED' for row in snapshot['rows'])):
            raise ValueError('MODEL_OUTCOMES_REVIEW_SNAPSHOT_MISMATCH')
        unresolved = {row['id'] for row in snapshot['rows'] if row['status'] == 'UNKNOWN'}
        if not unresolved or unresolved != set(review['unknown_ids']):
            raise ValueError('MODEL_OUTCOMES_REVIEW_UNKNOWN_MISMATCH')
        abnormal = {row['id'] for row in snapshot['rows'] if row['status'] != 'COMPLETE'}
        if set(review['records']) != abnormal:
            raise ValueError('MODEL_OUTCOMES_REVIEW_RECORD_SET_MISMATCH')
        for identity, expected_hash in review['records'].items():
            record_path = path.parent / 'physical-requests' / (identity + '.json')
            record = json.loads(record_path.read_text('utf-8'))
            row = next(row for row in snapshot['rows'] if row['id'] == identity)
            if (sha256(record_path.read_bytes()).hexdigest() != expected_hash or
                    record['status'] != row['status'] or record['physical_request_id'] != identity or
                    not record['category'].endswith(':model')):
                raise ValueError('MODEL_OUTCOMES_REVIEW_RECORD_CHANGED')
        return {**snapshot, 'reviewed_unknown_ids': sorted(unresolved),
                'review_path': str(review_path), 'review_sha256': review_hash,
                'unknown_reservations_retained': True}
    if (review.get('schema') != 'competitive-explicit-recovery/1' or
            review.get('old_unknown_remains_unknown_and_reserved') is not True or
            Path(review['root']).resolve() != path.parent or
            Path(review['snapshot']['path']).resolve() != path):
        raise ValueError('REVIEWED_PREDECESSOR_PLAN_MISMATCH')
    snapshot = _snapshot(path)
    unresolved = {row['id'] for row in snapshot['rows'] if row['status'] == 'UNKNOWN'}
    if (not unresolved or unresolved != set(review['unknown_ids']) or snapshot['summary']['over_budget'] or
            any(row['status'] == 'STARTED' for row in snapshot['rows'])):
        raise ValueError('REVIEWED_PREDECESSOR_UNEXPECTED_UNRESOLVED_OR_OVER_BUDGET')
    old = {row['id']: row for row in review['snapshot']['rows']}
    current = {row['id']: row for row in snapshot['rows']}
    markers = {item['old_physical_id']: item for item in review['reviewed_markers'].values()
               if item.get('kind') == 'UNKNOWN'}
    if set(markers) != unresolved:
        raise ValueError('REVIEWED_PREDECESSOR_UNKNOWN_MARKERS_MISMATCH')
    for identity in unresolved:
        record = path.parent / 'physical-requests' / (identity + '.json')
        if (current[identity] != old.get(identity) or
                sha256(record.read_bytes()).hexdigest() != markers[identity]['record_sha256']):
            raise ValueError('REVIEWED_PREDECESSOR_UNKNOWN_EVIDENCE_CHANGED')
    if sha256(review_path.read_bytes()).hexdigest() != review_hash:
        raise ValueError('REVIEWED_PREDECESSOR_REVIEW_CHANGED')
    return {**snapshot, 'reviewed_unknown_ids': sorted(unresolved),
            'review_path': str(review_path), 'review_sha256': review_hash,
            'unknown_reservations_retained': True}


def _validate_reviewed(frozen):
    _validate_frozen_identity(frozen)
    current = freeze_reviewed_predecessor(frozen['path'], frozen['review_path'])
    if (current['snapshot_sha256'] != frozen['snapshot_sha256'] or
            current['database_sha256'] != frozen['database_sha256'] or
            current['review_sha256'] != frozen['review_sha256'] or
            current['reviewed_unknown_ids'] != frozen['reviewed_unknown_ids']):
        raise ValueError('REVIEWED_PREDECESSOR_CHANGED')
    return current


class ReviewedPredecessorLedger(LinkedBudgetLedger):
    """Normal child budget admission with an exact, reviewed old UNKNOWN."""
    def _parents(self):
        return [_validate_reviewed(value) if value.get('reviewed_unknown_ids') else _validate_frozen(value)
                for value in self.predecessors]
