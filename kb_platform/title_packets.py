from collections import defaultdict
import re


def _normalized(value):
    return ' '.join(re.findall(r'[^\W_]+', value.casefold(), re.UNICODE))


def title_packet(question, documents, *, hops=2, limit=8):
    if type(hops) is not int or not 0 <= hops <= 3 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('INVALID_TITLE_PACKET_LIMIT')
    by_title = defaultdict(list)
    for document in documents:
        key = _normalized(document['title'])
        if key:
            by_title[key].append(document)
    initials = defaultdict(list)
    for document in documents:
        body = document['text'].split('\n', 1)[-1]
        name = re.match(r'^([A-Z][\w]+(?: [A-Z][\w]+){2,4})\s*\(', body)
        if name:
            parts = name[1].split()
            alias = ' '.join([*(p[0].casefold() for p in parts[:-1]), parts[-1].casefold()])
            initials[alias].append(document)
    selected, edges = {}, []
    frontier = [dict(source_id=None, text=question)]
    for hop in range(hops + 1):
        following = []
        for seed in frontier:
            text = ' ' + _normalized(seed['text']) + ' '
            matches = []
            for title, rows in by_title.items():
                if len(title) < 4 or ' ' + title + ' ' not in text:
                    continue


                literal = [d for d in rows if re.search(r'(?<!\w)' + re.escape(d['title']) + r'(?!\w)', seed['text'], re.I)]
                resolved = rows if len(rows) == 1 else literal
                if len(resolved) == 1:
                    matches.append((text.find(' ' + title + ' '), -len(title), resolved[0], 'title'))
            for alias, rows in initials.items():
                if len(rows) == 1 and ' ' + alias + ' ' in text:
                    matches.append((text.find(' ' + alias + ' '), -len(alias), rows[0], 'initials_search_hint'))
            for _, _, document, kind in sorted(matches, key=lambda item: (item[0], item[1], item[2]['source_id'])):
                sid = document['source_id']
                if sid in selected:
                    continue
                if len(selected) >= limit:
                    return list(selected.values()), edges
                selected[sid] = document
                edges.append(dict(from_source=seed['source_id'], to_source=sid,
                                  title=document['title'], hop=hop, hint_kind=kind, relation_verified=False))
                following.append(document)
        frontier = following
    return list(selected.values()), edges


def linked_evidence_packet(question, documents, *, seed_limit=3, hops=2):

    from .evidence_packing import blocks, rank_texts, terms
    from .cross_document import navigate_source_references

    if type(seed_limit) is not int or not 1 <= seed_limit <= 10:
        raise ValueError('INVALID_SEED_LIMIT')
    def spans(document):


        if not document.get('positions'):
            return blocks(document['text'])
        grouped = []
        for start, end in document['positions'].values():


            if grouped and document['text'][start:end].lstrip().startswith('|'):
                grouped[-1] = (grouped[-1][0], end)
            else:
                grouped.append((start, end))
        return grouped

    rows = [dict(source_id=d['source_id'], start=start, end=end, text=d['text'][start:end])
            for d in documents for start, end in spans(d)]
    query = terms(question)
    ranked = rank_texts([r['text'] for r in rows], question)
    seeds = [rows[i] for i in ranked if query & terms(rows[i]['text'])][:seed_limit]
    if not seeds:
        return [], {'seed_sources': [], 'unresolved_references': [], 'relations_verified': False}
    navigation = navigate_source_references(
        [dict(id=d['source_id'], title=d['title'], text=d['text'], paragraph_spans=list(spans(d))) for d in documents],
        list(dict.fromkeys(s['source_id'] for s in seeds)), hops=hops, document_limit=16, fragment_limit=40)
    selected = {(r['source_id'], r['start'], r['end']): r for r in seeds}
    for fragment in navigation['fragments']:
        row = dict(source_id=fragment['doc_id'], start=fragment['start'], end=fragment['end'], text=fragment['text'])
        selected.setdefault((row['source_id'], row['start'], row['end']), row)
    return list(selected.values()), {'seed_sources': [s['source_id'] for s in seeds],
        'navigation_span_count': len(navigation['fragments']),
        'unresolved_references': navigation['unresolved_references'], 'relations_verified': False}
