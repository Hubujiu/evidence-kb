"""Current product gateway + pipeline + context over frozen originals.

Only search/storage transport is an in-memory lexical fixture. Gold is passed
exclusively to the unified runner. No model or embedding calls are made.
"""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from kb_platform.evidence_packing import rank_texts, terms
from .contracts import Suite, Plan, Fragment, RequestEvidenceBinding, digest
from .evidence_experiment import PacketEvidenceAdapter, read
from .product_fixture import Authority, Gateway
from .runner import run, write_json
from .scoring import corpus_hash


class ProductAdapter(PacketEvidenceAdapter):
    def __init__(self, catalog, enabled, service=False):
        super().__init__(12000, catalog=catalog)
        self.enabled = enabled
        self.service = service
        files = ['retrieval_pipeline.py', 'agent_tools.py', 'documents.py', 'delivery_units.py', 'agent_service.py']
        self.implementation = digest(dict(base=self.implementation, enabled=enabled, service=service,
            files={name: hashlib.sha256((Path(__file__).parents[1] / name).read_bytes()).hexdigest() for name in files},
            adapter=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            fixture=hashlib.sha256(Path(__file__).with_name('product_fixture.py').read_bytes()).hexdigest()))
        self.products = {}

    def run(self, request, sources, corpus):
        catalog = {row['id']: row for row in self.catalog}
        for source in sources:
            if catalog[source.id]['text'] != source.text:
                raise ValueError('CATALOG_ORIGINAL_MISMATCH')
        authority = Authority([(catalog[s.id]['title'] + '.md', s.text) for s in sources])
        gateway = Gateway(authority)
        original = {f's{i}': source for i, source in enumerate(sources)}

        def search(index, tenant, scope, allowed, query, limit, **kwargs):
            permitted = {row['source_id'] for row in allowed}
            excluded = set(kwargs.get('exclude_citation_ids', ()))
            units = [u for u in authority.units if u['source_id'] in permitted and u['kind'] != 'Page'
                     and u['citation_id'] not in excluded]
            order = rank_texts([u['text'] for u in units], query)
            return [{**units[i], 'score': 1.0 / (rank + 1)} for rank, i in enumerate(order)
                    if terms(query) & terms(units[i]['text'])][:limit]

        authority.backend.search = search
        result = gateway.search_pipeline(authority.actor, authority.scope, request.question, {},
            {'title_navigation': self.enabled, 'max_expanded_units': 16})
        packet, spans = [], []
        for hit in result['hits']:
            source = original[hit['source_id']]
            start = source.text.find(hit['text'])
            if start < 0 or source.text.find(hit['text'], start + 1) >= 0:
                raise ValueError('CANONICAL_UNIT_NOT_UNIQUELY_BOUND_TO_FROZEN_ORIGINAL')
            packet.append(dict(source_id=source.id, text=hit['text']))
            spans.append(dict(start=start, end=start + len(hit['text'])))
        self.products[request.case_id] = result['trace']
        trace = self.assemble(request, sources, corpus, packet, result['trace'], spans)
        if not self.service:
            return trace
        from kb_platform.agent_service import AgentService
        from kb_platform.agent_api import install_agent_routes
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from .default_path_contracts import ScriptedModel
        gateway.authorize = lambda actor, scope: dict(scope_id=scope, manifest_id='build', authority_revision=authority.revision)
        gateway.sources = lambda actor, scope: [dict(source_id=r['source_id'], doc_id=r['source_id'],
            source_version_id=r['source_version_id'], title=r['filename']) for r in authority.rows]
        model = ScriptedModel(query=request.question)
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(None, model, root=Path(folder), gateway=gateway,
                retrieval_config={'channels': ['bm25'], 'rerank': False})
            app = FastAPI()
            install_agent_routes(app, authority, None, lambda: authority.actor)
            app.state.agent = service
            with TestClient(app) as client:
                created = client.post(f'/scopes/{authority.scope}/sessions', json={})
                created.raise_for_status()
                session = created.json()
                response = client.post(f"/agent/sessions/{session['session_id']}/turns", json={
                    'message': request.question, 'request_id': 'evidence-probe', 'expected_revision': 0})
                response.raise_for_status()
                outcome = response.json()
        self.products[request.case_id]['service'] = outcome['trace']
        answer_calls = [c for c in model.calls if c['role'] == 'answer']
        if not answer_calls:
            return trace.model_copy(update={'status': 'ERROR', 'error': outcome.get('stop_reason', 'NO_ANSWER_INPUT'),
                                            'final': (), 'final_context': '', 'request_messages': ()})
        messages = answer_calls[-1]['messages']
        projection = json.loads(messages[-1]['content'])
        final, bindings = [], []
        for index, item in enumerate(projection['evidence']):
            if not item.get('text'):
                continue
            source = original[item['source_id']]
            start = source.text.find(item['text'])
            if start < 0 or source.text.find(item['text'], start + 1) >= 0:
                raise ValueError('MODEL_INPUT_NOT_UNIQUELY_BOUND_TO_ORIGINAL')
            final.append(Fragment(source_id=source.id, source_sha256=source.sha256, version=source.version,
                kb=source.kb, start=start, end=start+len(item['text']), text=item['text']))
            bindings.append(RequestEvidenceBinding(message_index=len(messages)-1,
                json_path=('evidence', index, 'text'), fragment_index=len(final)-1))
        self.observations[request.case_id]['service_messages'] = messages
        return trace.model_copy(update={'final': tuple(final), 'stages': {**trace.stages, 'assembly': tuple(final)},
            'final_context': json.dumps([dict(source_id=f.source_id, text=f.text) for f in final], ensure_ascii=False),
            'request_evidence_bindings': tuple(bindings), 'request_messages': tuple(messages)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service', action='store_true')
    args = parser.parse_args()
    suite = Suite.model_validate(read(args.suite))
    if args.service:
        # Bound the complete serialized message envelope, not just the evidence
        # projection used by component comparisons. Annotation paths are intact.
        suite = suite.model_copy(update={'id': suite.id + '/service-wire-24000', 'cases': tuple(
            c.model_copy(update={'input': c.input.model_copy(update={'context_budget': 24000})}) for c in suite.cases)})
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {}
    for enabled in ((True,) if args.service else (False, True)):
        adapter = ProductAdapter(read(args.catalog), enabled, args.service)
        traces = [adapter.run(case.input, tuple(s for s in suite.sources if s.id in case.input.allowed_sources),
                              corpus_hash(suite)) for case in suite.cases]
        directory = args.output / ('default-on' if enabled else 'navigation-off')
        report = run(suite, Plan(suite_hash=digest(suite), objective='evidence', mode='replay',
                                required_stages=('retrieval', 'assembly')), directory, traces=traces)
        write_json(directory / 'product-traces.json', adapter.products)
        write_json(directory / 'component-observations.json', adapter.observations)
        summary[directory.name] = {key: report[key] for key in ('total', 'counts', 'final_complete_rate')}
        print(directory.name, summary[directory.name], flush=True)
    write_json(args.output / 'summary.json', dict(results=summary, new_remote_calls=0,
        scope='current gateway/pipeline/context; in-memory storage and lexical backend, not live HTTP or provider'))


if __name__ == '__main__':
    main()
