import argparse
import hashlib
import json
from pathlib import Path

from .contracts import Suite, digest
from .runner import write_json


CASES = {
    'missing_place': '2wiki-438a3de40bdb11eba7f7acde48001122',
    'wrong_person': '2wiki-f418184a08be11ebbd8aac1f6bf848b6',
    'england': '2wiki-b1a94a9a0bdb11eba7f7acde48001122',
    'french': '2wiki-d18fc6640bda11eba7f7acde48001122',
    'grandfather': '2wiki-1bb1993c0bb011ebab90acde48001122',
}


def revise(suite, gold):
    data = suite.model_dump(mode='json')
    data['id'] += '/human-adjudicated-v2'
    evidence = {e['id']: e for e in data['evidence']}
    sources = {s.id: s for s in suite.sources}
    gold = json.loads(json.dumps(gold))
    changes = []
    for case in data['cases']:
        cid = case['input']['case_id']
        case['split'] = 'dev'
        if cid in (CASES['missing_place'], CASES['wrong_person']):
            case['expected_behavior'], case['any_of'] = 'abstain', []
            case['qrels'] = {}
            gold['cases'][cid]['answers'] = ['INSUFFICIENT_EVIDENCE']
            gold['cases'][cid]['adjudicated_expected_behavior'] = 'abstain'
            if cid == CASES['wrong_person']:
                wrong = next(s for s in suite.sources if s.text.startswith('# Tony Williams (drummer)\n'))
                case['forbidden_text'].append(wrong.text.split('\n', 1)[1].split(' Williams first')[0])
            changes.append(dict(case_id=cid, ruling='insufficient_source_evidence', original_gold_preserved=True))
        elif cid == CASES['england']:
            gold['cases'][cid]['answers'] = ['British', 'England']
            sid = next(s.id for s in suite.sources if s.text.startswith('# Donald Winnicott\n'))
            source = sources[sid]
            quote = source.text.split('\n', 1)[1].split('. He was')[0] + '.'
            eid = sid + ':human-v2:nationality'
            start = source.text.index(quote)
            data['evidence'].append(dict(id=eid, source_id=sid, start=start, end=start+len(quote),
                quote=quote, fact='English nationality stated in original biography', qualifiers=['English']))
            case['any_of'] = [[eid if evidence[e]['source_id'] == sid else e for e in group] for group in case['any_of']]
            changes.append(dict(case_id=cid, ruling='accept_England_case_specific', evidence_rebound_to_explicit_nationality=True))
        elif cid == CASES['french']:
            gold['cases'][cid]['answers'] = ['France', 'French']
            changes.append(dict(case_id=cid, ruling='accept_French_case_specific'))
        elif cid == CASES['grandfather']:
            gold['cases'][cid]['answers'] = ['Abd al-Malik ibn Marwan', 'Abd al-Malik']
            changes.append(dict(case_id=cid, ruling='accept_Abd_al_Malik_only_in_this_case', father_still_wrong=True))
    for value in gold['cases'].values():
        value.setdefault('answers', [value['answer']])
    return Suite.model_validate(data), gold, changes


def main():
    parser = argparse.ArgumentParser(description='Explicit human-adjudicated annotation revision; old artifacts remain read-only.')
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    original = Suite.model_validate_json(args.suite.read_text('utf-8'))
    gold = json.loads(args.gold.read_text('utf-8'))
    revised, revised_gold, changes = revise(original, gold)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / 'suite.json', revised.model_dump(mode='json'))
    write_json(args.output / 'scorer-gold.json', revised_gold)
    write_json(args.output / 'adjudication.json', dict(version='human-adjudicated-v2',
        authority='Explicit user approval in this chat, 2026-10-28 Asia/Shanghai',
        instruction='这5项都按你的建议裁定，保留旧记录，形成新的标注版本',
        objective='final context evidence correctness/completeness; generation not an acceptance gate',
        old_suite_hash=digest(original), new_suite_hash=digest(revised), changes=changes,
        annotation_provenance='public annotations plus five explicit human adjudications; not full business gold',
        sources_unchanged=original.sources == revised.sources,
        dependencies={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.suite, args.gold)},
        independent_holdout=False, new_calls=0))
    print(json.dumps(dict(output=str(args.output), changes=len(changes), cases=len(revised.cases))))


if __name__ == '__main__':
    main()
