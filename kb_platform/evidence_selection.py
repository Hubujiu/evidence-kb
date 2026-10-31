from __future__ import annotations

from collections import Counter
import math

from .evidence_packing import terms


def _identity(item):
    return tuple(item.get(key) for key in
                 ('source_id', 'source_version_id', 'parse_snapshot_id', 'build_id', 'original_sha256'))


def _range(item):
    start, end = item.get('parse_text_start'), item.get('parse_text_end')
    if type(start) is int and type(end) is int and 0 <= start < end and end - start == len(item.get('text', '')):
        return start, end
    return None


def deduplicate_evidence(items):

    rows = list({item['citation_id']: item for item in items}.values())
    kept = []
    for index, item in enumerate(rows):
        text = item.get('text', '')
        duplicate = False
        for other_index, other in enumerate(rows):
            if index == other_index or _identity(item) != _identity(other):
                continue
            if (text == other.get('text') and item.get('anchors') == other.get('anchors') and
                    item.get('evidence_ids') == other.get('evidence_ids') and other_index > index):
                duplicate = True
                break
            left, right = _range(item), _range(other)
            if (left and right and not item.get('table_data') and item.get('kind') != 'Table' and
                    right[0] <= left[0] < left[1] <= right[1] and right != left and
                    other['text'][left[0] - right[0]:left[1] - right[0]] == text):
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept


def select_evidence(items, limit, question, *, subquestions=(), preferred_ids=()):

    if type(limit) is not int or limit < 1:
        raise ValueError('INVALID_ACTIVE_EVIDENCE_LIMIT')
    rows = deduplicate_evidence(items)
    if not rows:
        return []
    goals = [terms(q) for q in [question, *subquestions] if isinstance(q, str) and q.strip()]
    goals = [goal for goal in goals if goal]
    bags = [terms(row.get('text', '')) for row in rows]
    frequency = Counter(term for bag in bags for term in bag)
    weights = {term: math.log1p(len(rows) / count) for term, count in frequency.items()}
    preferred = set(preferred_ids)
    covered = [set() for _ in goals]
    selected, remaining = [], list(range(len(rows)))
    while remaining and len(selected) < limit:
        def score(index):
            bag = bags[index]
            gains = [sum(weights.get(t, 0) for t in (goal & bag) - seen) / max(1, len(goal))
                     for goal, seen in zip(goals, covered)]
            new_goals = sum(bool(goal & bag) and not seen for goal, seen in zip(goals, covered))
            relevance = sum(weights.get(t, 0) for goal in goals for t in goal & bag)
            confirmed = int(rows[index]['citation_id'] in preferred and relevance > 0)
            return new_goals, sum(gains), confirmed, relevance, index
        chosen = max(remaining, key=score)
        selected.append(chosen)
        remaining.remove(chosen)
        for goal, seen in zip(goals, covered):
            seen.update(goal & bags[chosen])
    return [rows[index] for index in selected]


def prioritize_cited_sources(fragments, response):

    baseline = list(dict.fromkeys(fragment['source_id'] for fragment in fragments))
    if not response.get('claims'):
        return baseline
    bindings = {}
    for item in response.get('evidence', []):
        identity = item['citation_id']
        if identity in bindings:
            raise ValueError('AMBIGUOUS_CITED_EVIDENCE_ID')
        bindings[identity] = item
    selected = []
    for claim in response['claims']:
        identifiers = claim['evidence_ids']
        quotes = claim['quotes']
        if not identifiers or set(q['evidence_id'] for q in quotes) != set(identifiers):
            raise ValueError('CITED_EVIDENCE_QUOTES_MISMATCH')
        for identity in identifiers:
            if identity not in bindings:
                raise ValueError('CITED_EVIDENCE_NOT_BOUND')
            item = bindings[identity]
            available = [f for f in fragments if f['source_id'] == item['source_id'] and
                f['source_sha256'] == item['original_sha256'] and
                f['version'] == item['source_version_id'] and f.get('kind', 'extract') == 'extract']
            if not available or any(not q['quote'] or not any(q['quote'] in f['text'] for f in available)
                                    for q in quotes if q['evidence_id'] == identity):
                raise ValueError('CITED_ORIGINAL_NOT_VISIBLE')
            if item['source_id'] not in selected:
                selected.append(item['source_id'])
    return selected + [identity for identity in baseline if identity not in selected]
