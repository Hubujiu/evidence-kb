import re
from hashlib import sha256


def restore_events(events, args, state, current_seq, config):
    if config.get('chinese_history_terms'):
        from .evidence_packing import terms as query_terms
        terms = query_terms(args['query'])
    else:
        terms = set(re.findall(r'\w+', args['query'].casefold()))
    kinds = {'user', 'assistant', 'tool_result'} if config.get('expand_history') else {'user'}
    candidates = [e for e in events if e['kind'] in kinds
                  and e['payload'].get('source_epoch') == state['source_epoch'] and e['seq'] != current_seq
                  and (not args.get('event_seq') or e['seq'] == args['event_seq'])
                  and (e['kind'] != 'assistant' or
                       set(e['payload'].get('evidence_ids', [])) <= set(state['evidence']))]
    ranked = sorted(candidates, key=lambda e: (
        sum(term in str(e['payload']).casefold() for term in terms), e['seq']), reverse=True)
    restored = []
    for item in ranked[:3]:
        raw = item['payload'].get('text') or str(item['payload'].get('result', ''))
        offsets = [raw.casefold().find(term) for term in terms if term in raw.casefold()]
        offset = args.get('offset', 0) if args.get('event_seq') else max(0, min(offsets, default=0) - 300)
        offset = min(max(0, offset), len(raw))
        restored.append({'seq': item['seq'], 'kind': item['kind'], 'text': raw[offset:offset + 1500],
                         'offset': offset, 'truncated': offset != 0 or len(raw) > 1500,
                         'full_text_sha256': sha256(raw.encode('utf-8')).hexdigest(),
                         'original_characters': len(raw)})
    return restored
