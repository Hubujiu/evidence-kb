"""Bounded single-session working state; tool receipts, never invented facts."""
from copy import deepcopy


def begin_work_state(previous, message, constraints, epoch):
    old = previous if isinstance(previous, dict) and previous.get('source_epoch') == epoch else {}
    return {'protocol': 'single-session-work/1', 'revision': old.get('revision', 0) + 1,
            'source_epoch': epoch, 'objective': old.get('objective', message), 'current_request': message,
            'constraints': deepcopy(constraints),
            'resolved_query': message, 'subquestions': [], 'missing_information': [],
            'confirmed_evidence_ids': list(old.get('confirmed_evidence_ids', []))[-8:],
            'completed_tools': [], 'status': 'planning'}


def record_decision(work, decision):
    query = decision.get('query')
    if isinstance(query, str) and query.strip():
        work['resolved_query'] = query[:2000]
    for key in ('subquestions', 'missing_information'):
        value = decision.get(key, work.get(key, []))
        if isinstance(value, list) and len(value) <= 6 and all(isinstance(x, str) and len(x) <= 500 for x in value):
            work[key] = value[:]
    work['sufficiency'] = decision.get('sufficiency', 'unknown')
    work['status'] = 'planning' if decision['action'] in {'answer', 'clarify'} else 'executing'


def record_receipt(work, output):
    if output['status'] == 'ok':
        work['completed_tools'].append({'name': output['name'], 'args': deepcopy(output['args']),
                                        'evidence_ids': output['result'].get('evidence_ids', [])})
        work['completed_tools'] = work['completed_tools'][-4:]
    else:
        work['status'] = 'tool_failed'


def finish_work_state(work, status, claims):
    work['status'] = status
    work['confirmed_evidence_ids'] = list(dict.fromkeys(
        eid for claim in claims for eid in claim.get('evidence_ids', [])))[-8:]
