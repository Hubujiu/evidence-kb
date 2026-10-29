from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.title_packets import title_packet, linked_evidence_packet
from .contracts import Fragment, Plan, Suite, Trace, digest
from .runner import run, write_json
from .scoring import corpus_hash


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def load_traces(directory):

    manifest = read(directory / 'manifest.json')
    for name, expected in manifest.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError('HISTORICAL_MANIFEST_MISMATCH:' + name)
    return [Trace.model_validate(read(p)) for p in sorted((directory / 'traces').glob('*.json'))]


class PacketEvidenceAdapter:
    def __init__(self, budget=16000, recorded_sources=None, catalog=None):
        self.budget = budget
        self.recorded_sources = recorded_sources
        self.catalog = catalog
        paths = [Path(__file__), Path(__file__).parents[1] / 'title_packets.py',
                 Path(__file__).parents[1] / 'agent_context.py', Path(__file__).parents[1] / 'evidence_packing.py',
                 Path(__file__).parents[1] / 'prose_windows.py', Path(__file__).parents[1] / 'cross_document.py']
        self.implementation = digest(dict(files={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            input_budget=budget, recorded_sources=recorded_sources, catalog=catalog,
            protocol='title-navigation-current-context/2', under_test=True))
        self.observations = {}

    def run(self, request, sources, corpus):
        docs = [dict(source_id=s.id, title=s.text.split('\n', 1)[0].lstrip('# ').strip(), text=s.text) for s in sources]
        packet, edges = title_packet(request.question, docs)
        spans = None
        if self.catalog is not None:
            catalog = {d['id']: d for d in self.catalog}
            docs = []
            for source in sources:
                record = catalog[source.id]
                if record['text'] != source.text:
                    raise ValueError('SOURCE_CATALOG_ORIGINAL_MISMATCH')
                docs.append(dict(source_id=source.id, title=record['title'], text=source.text))
            spans, edges = linked_evidence_packet(request.question, docs)
            packet = [dict(source_id=s['source_id'], text=s['text']) for s in spans]
        if self.recorded_sources is not None:
            ids = self.recorded_sources[request.case_id]
            by_id = {d['source_id']: d for d in docs}
            if not set(ids) <= by_id.keys():
                raise ValueError('RECORDED_SOURCE_NOT_CURRENTLY_AUTHORIZED')
            packet, edges = [by_id[sid] for sid in ids], []
        return self.assemble(request, sources, corpus, packet, edges, spans)

    def assemble(self, request, sources, corpus, packet, edges, spans=None):
        allowed = {s.id: s for s in sources}
        candidates, records = [], []
        for index, document in enumerate(packet):
            s = allowed[document['source_id']]
            if not s.text:
                continue
            start, end = (spans[index]['start'], spans[index]['end']) if spans is not None else (0, len(s.text))
            candidates.append(Fragment(source_id=s.id, source_sha256=s.sha256, version=s.version,
                kb=s.kb, start=start, end=end, text=s.text[start:end]))
            records.append(dict(source_id=s.id, citation_id=f'{s.id}:{start}:{end}', source_version_id=s.version,
                original_sha256=s.sha256, text=s.text[start:end], parse_text_start=start, parse_text_end=end))
        prepared = ContextManager(ContextPolicy(input_budget=self.budget, output_reserve=4096,
            model_window=131072, safety_reserve=8192, evidence_strategy='query_blocks')).prepare(
                'answer', 'Use original source evidence. Source mentions do not establish entity identity.',
                {}, [], records, request.question)
        final = []
        for item in prepared.projection['evidence']:
            if item.get('text'):
                source = allowed[item['source_id']]
                start = item['parse_text_start'] + item['text_start']
                final.append(Fragment(source_id=source.id, source_sha256=source.sha256, version=source.version,
                    kb=source.kb, start=start, end=start+len(item['text']), text=item['text']))
        context = json.dumps(prepared.projection['evidence'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))


        self.observations[request.case_id] = dict(prepared=prepared.to_dict(), edges=edges,
            source_count=len(packet), context_size=len(context), capture='replay', new_remote_calls=0)
        return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
            principal=request.principal, acl_revision=request.acl_revision,
            stages={'retrieval': tuple(candidates), 'assembly': tuple(final)}, final=tuple(final),
            final_context=context, request_messages=prepared.messages, capture='replay')


def execute(suite, output, budget=16000, recorded_sources=None, catalog=None):
    adapter = PacketEvidenceAdapter(budget, recorded_sources, catalog)
    traces = []
    for case in suite.cases:
        authorized = tuple(s for s in suite.sources if s.id in case.input.allowed_sources)
        traces.append(adapter.run(case.input, authorized, corpus_hash(suite)))
    report = run(suite, Plan(suite_hash=digest(suite), mode='replay', objective='evidence',
                 required_stages=('retrieval', 'assembly')), output, traces=traces)
    write_json(output / 'component-observations.json', adapter.observations)
    return report


def main():
    parser = argparse.ArgumentParser(description='Current evidence components and revised-label replay; never calls a model.')
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--history', type=Path, action='append', default=[])
    parser.add_argument('--source-catalog', type=Path)
    args = parser.parse_args()
    suite = Suite.model_validate(read(args.suite))
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {}
    for directory in args.history:
        old = Suite.model_validate(read(directory / 'suite.json'))
        if suite.sources != old.sources:
            raise ValueError('CORPUS_CHANGED_NOT_A_LABEL_ONLY_REPLAY')

        budgets = {c.input.case_id: c.input.context_budget for c in old.cases}
        revised = suite.model_copy(update={'cases': tuple(c.model_copy(update={'input': c.input.model_copy(
            update={'context_budget': budgets.get(c.input.case_id, c.input.context_budget)})}) for c in suite.cases)})
        old_plan = Plan.model_validate(read(directory / 'plan.json'))
        plan = Plan(suite_hash=digest(revised), mode='replay', objective='evidence',
                    required_stages=old_plan.required_stages)
        name = directory.parent.name + '-' + directory.name
        report = run(revised, plan, args.output / name, traces=load_traces(directory))
        summary[name] = {k: report[k] for k in ('total', 'counts', 'final_complete_rate')}
        if len(old.cases) == len(suite.cases) and all(c.input.context_budget == 16000 for c in old.cases):
            original_traces = load_traces(directory)
            if all(t.status == 'OK' for t in original_traces):
                selected = {t.case_id: list(dict.fromkeys(f.source_id for f in t.final)) for t in original_traces}
                paired = execute(suite, args.output / (name + '-current-assembly'), recorded_sources=selected)
                summary[name + '-current-assembly'] = {k: paired[k] for k in ('total', 'counts', 'final_complete_rate')}
    report = execute(suite, args.output / 'candidate', catalog=read(args.source_catalog) if args.source_catalog else None)
    summary['candidate'] = {k: report[k] for k in ('total', 'counts', 'final_complete_rate')}
    write_json(args.output / 'summary.json', dict(results=summary, new_model_embedding_judge_calls=0,
        scope='current component over frozen authorized corpus; replay, not actual model send or live ACL',
        full_coverage_denominator=sum(c.expected_behavior == 'answer' for c in suite.cases),
        fixed_case_denominator=len(suite.cases), independent_acceptance=False))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
