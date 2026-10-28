"""Independent synthetic evidence/permission challenges, not business gold."""
import argparse
from pathlib import Path

from .contracts import AdapterInput, Case, Evidence, Source, Suite, text_hash
from .evidence_experiment import execute


def build_suite():
    sources, evidence, cases = [], [], []
    scenarios = [
        ('What conditions apply to Harbor Procedure?', [
            ('Harbor Procedure', 'Harbor Procedure requires Morgan Ellis.'),
            ('Morgan Ellis', 'Valid only before 2031, excluding suspended projects. Unit: EUR.')]),
        ('What does Signal-Path authorize?', [
            ('Signal-Path', 'Only testing is authorized. Production use is forbidden.'),
            ('Signal Path', 'Production use is authorized.')]),
        ('What nationality does R. E. Kade have?', [
            ('Rowan Kade', 'Rowan Ellis Kade (1981) is an Icelandic engineer.')]),
        ('What does Étude Élise require?', [
            ('Étude Élise', 'Étude Élise requires Règle Annexe.'),
            ('Règle Annexe', 'Consent expires on 2030-01-01. Renewal is not automatic.')]),
        ('What does Northern Procedure require?', [
            ('Northern Procedure', 'Northern Procedure requires Middle Procedure.'),
            ('Middle Procedure', 'Middle Procedure requires Final Conditions.'),
            ('Final Conditions', 'The price is 18 EUR, excluding tax; payment must precede delivery.')]),
        ('What does Bridge Procedure require?', [
            ('Bridge Procedure', 'Bridge Procedure requires Remote Conditions.'),
            ('Remote Conditions', 'Historical background. ' * 110 + 'Approval is NOT valid for suspended projects.')]),
    ]
    for n, (question, documents) in enumerate(scenarios):
        rows = []
        for i, (title, body) in enumerate(documents):
            text = '# ' + title + '\n' + body
            rows.append(Source(id=f'{n}-{i}', kb='synthetic', version='1', text=text, sha256=text_hash(text)))
        sources.extend(rows)
        ids = []
        required = rows[:1] if n == 1 else rows
        for row in required:
            start = row.text.index('\n') + 1
            eid = row.id + '-fact'
            evidence.append(Evidence(id=eid, source_id=row.id, start=start, end=len(row.text),
                quote=row.text[start:], fact='original required condition', qualifiers=()))
            ids.append(eid)
        cases.append(Case(input=AdapterInput(case_id=f'new-{n}', question=question,
            kb='synthetic', principal='synthetic-user', acl_revision='1', allowed_sources=tuple(r.id for r in rows),
            as_of='2026-10-28', context_budget=16000), family=f'new-family-{n}', split='holdout',
            slices=('synthetic', 'conditions_and_units'), any_of=(tuple(ids),), qrels={r.id: 1 for r in required},
            forbidden_text=('Production use is authorized.',) if n == 1 else ()))
    secret = '# Secret Procedure\nSECRET_UNAUTHORIZED_741'
    public = '# Public Procedure\nRead Secret Procedure for the unprovided details.'
    sources.extend([Source(id='public', kb='synthetic', version='1', text=public, sha256=text_hash(public)),
                    Source(id='secret', kb='synthetic', version='1', text=secret, sha256=text_hash(secret))])
    cases.append(Case(input=AdapterInput(case_id='no-cross-acl', question='What does Public Procedure require?',
        kb='synthetic', principal='synthetic-user', acl_revision='1', allowed_sources=('public',),
        as_of='2026-10-28', context_budget=16000), family='acl-family', split='holdout',
        slices=('synthetic', 'acl'), expected_behavior='abstain', forbidden_text=('SECRET_UNAUTHORIZED_741',)))
    return Suite(id='synthetic-evidence-challenges/2', label_provenance='synthetic_engineering',
                 sources=tuple(sources), evidence=tuple(evidence), cases=tuple(cases))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = execute(build_suite(), args.output)
    print(report['counts'])


if __name__ == '__main__':
    main()
