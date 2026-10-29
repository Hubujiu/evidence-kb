import argparse
from dataclasses import asdict
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


def replay_application(authority, model, gateway, root, context_policy):

    from kb_platform.knowledge_profile import create_knowledge_application, load_knowledge_profile
    from kb_platform.plugin_host import PluginSpec

    profile = load_knowledge_profile(environment={})
    profile['plugins'] = [row for row in profile['plugins'] if row['id'] not in {'provider', 'budget'}]
    by_id = {row['id']: row for row in profile['plugins']}
    by_id['model'].update(use='replay.model', config={})
    by_id['gateway'].update(use='replay.gateway', config={})
    by_id['store']['config']['root'] = str(root)
    by_id['knowledge']['config']['retrieval_config'] = {'channels': ['bm25'], 'rerank': False}
    if context_policy is not None:
        by_id['policy']['config'] = asdict(context_policy)
    fixtures = (
        PluginSpec('replay.model', ('knowledge.model',), (),
                   lambda ctx, config: ctx.provide('knowledge.model', model)),
        PluginSpec('replay.gateway', ('knowledge.gateway',), (),
                   lambda ctx, config: ctx.provide('knowledge.gateway', gateway)),
    )
    return create_knowledge_application(authority, None, profile=profile,
                                        extra_plugins=fixtures, environment={})


def original_start(source, item):
    start = item.get('parse_text_start', 0) + item.get('text_start', 0)
    if source.text[start:start + len(item['text'])] == item['text']:
        return start
    start = source.text.find(item['text'])
    if start < 0 or source.text.find(item['text'], start + 1) >= 0:
        raise ValueError('CANONICAL_UNIT_NOT_UNIQUELY_BOUND_TO_FROZEN_ORIGINAL')
    return start


class ProductAdapter(PacketEvidenceAdapter):
    def __init__(self, catalog, enabled, service=False, *, backend=None, inventory=(), context_policy=None):
        super().__init__(12000, catalog=catalog)
        self.enabled = enabled
        self.service = service
        self.backend, self.inventory = backend, inventory
        self.indexes = {}
        self.context_policy = context_policy
        product_root = Path(__file__).parents[1]
        repository = product_root.parent
        files = sorted(product_root.glob('*.py')) + sorted((repository / 'rust').rglob('*.rs'))
        files += sorted((repository / 'rust').rglob('Cargo.toml'))
        files += sorted((repository / 'config/agent-profiles').glob('*.json'))
        files += [repository / name for name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'uv.lock')]
        self.implementation = digest(dict(base=self.implementation, enabled=enabled, service=service,
            files={path.relative_to(repository).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
            adapter=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            fixture=hashlib.sha256(Path(__file__).with_name('product_fixture.py').read_bytes()).hexdigest()))
        self.products = {}

    def run(self, request, sources, corpus):
        catalog = {row['id']: row for row in self.catalog}
        for source in sources:
            if catalog[source.id]['text'] != source.text:
                raise ValueError('CATALOG_ORIGINAL_MISMATCH')
        indexed = tuple(s for s in self.inventory if s.kb == request.kb) if self.backend else sources
        authority = Authority([(catalog[s.id]['title'] + '.md', s.text) for s in indexed])
        gateway = Gateway(authority)
        original = {f's{i}': source for i, source in enumerate(indexed)}

        def search(index, tenant, scope, allowed, query, limit, **kwargs):
            permitted = {row['source_id'] for row in allowed}
            excluded = set(kwargs.get('exclude_citation_ids', ()))
            units = [u for u in authority.units if u['source_id'] in permitted and u['kind'] != 'Page'
                     and u['citation_id'] not in excluded]
            order = rank_texts([u['text'] for u in units], query)
            return [{**units[i], 'score': 1.0 / (rank + 1)} for rank, i in enumerate(order)
                    if terms(query) & terms(units[i]['text'])][:limit]

        if self.backend:
            from uuid import uuid4
            authority.backend = self.backend
            key = digest(authority.units)
            name = self.indexes.get(key)
            if name is None:
                name = self.backend.index_name(authority.actor.tenant_id, authority.scope, uuid4().hex)
                self.backend.stage(name, authority.units)
                self.indexes[key] = name
            authority.build['index_name'] = name
            for row in authority.rows:
                if original[row['source_id']].id not in request.allowed_sources:
                    row['owner_id'] = 'denied'
        else:
            authority.backend.search = search
        result = gateway.search_pipeline(authority.actor, authority.scope, request.question, {},
            {'title_navigation': self.enabled, 'max_expanded_units': 16})
        packet, spans = [], []
        for hit in result['hits']:
            source = original[hit['source_id']]
            start = original_start(source, hit)
            packet.append(dict(source_id=source.id, text=hit['text']))
            spans.append(dict(start=start, end=start + len(hit['text'])))
        self.products[request.case_id] = result['trace']
        trace = self.assemble(request, sources, corpus, packet, result['trace'], spans)
        if not self.service:
            return trace
        from kb_platform.agent_api import SessionCreate, TurnRequest, install_agent_routes
        from fastapi import FastAPI
        from .default_path_contracts import ScriptedModel
        gateway.authorize = lambda actor, scope: dict(scope_id=scope, manifest_id='build', authority_revision=authority.revision)
        gateway.sources = lambda actor, scope: [dict(source_id=r['source_id'], doc_id=r['source_id'],
            source_version_id=r['source_version_id'], title=r['filename']) for r in authority.rows]
        model = ScriptedModel(query=request.question)
        with tempfile.TemporaryDirectory() as folder:
            application, host = replay_application(authority, model, gateway, folder, self.context_policy)
            app = FastAPI()
            install_agent_routes(app, authority, None, lambda: authority.actor)
            app.state.agent, app.state.agent_plugins = application, host
            routes = {route.path: route.endpoint for route in app.routes}
            with host:
                session = routes['/scopes/{scope_id}/sessions'](
                    scope_id=authority.scope, body=SessionCreate.model_validate({}), actor=authority.actor)
                outcome = routes['/agent/sessions/{session_id}/turns'](
                    session_id=session['session_id'], actor=authority.actor,
                    body=TurnRequest.model_validate({'message': request.question,
                        'request_id': 'evidence-probe', 'expected_revision': 0}))
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
            start = original_start(source, item)
            final.append(Fragment(source_id=source.id, source_sha256=source.sha256, version=source.version,
                kb=source.kb, start=start, end=start+len(item['text']), text=item['text']))
            bindings.append(RequestEvidenceBinding(message_index=len(messages)-1,
                json_path=('evidence', index, 'text'), fragment_index=len(final)-1))
        self.observations[request.case_id]['service_messages'] = messages
        return trace.model_copy(update={'final': tuple(final), 'stages': {**trace.stages, 'assembly': tuple(final)},
            'capture': 'pre_send',
            'final_context': json.dumps([dict(source_id=f.source_id, text=f.text) for f in final], ensure_ascii=False),
            'request_evidence_bindings': tuple(bindings), 'request_messages': tuple(messages)})


def main():
    parser = argparse.ArgumentParser(description='Current product gateway + pipeline + context over frozen originals.\n\nOnly search/storage transport is an in-memory lexical fixture. Gold is passed\nexclusively to the unified runner. No model or embedding calls are made.\n')
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--service', action='store_true')
    args = parser.parse_args()
    suite = Suite.model_validate(read(args.suite))
    if args.service:


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
        scope=('configured plugin host/native loop and registered route/request models with scripted model; '
               'current gateway/pipeline/context with in-memory storage and lexical backend; '
               'not live HTTP or provider' if args.service else
               'current gateway/pipeline/context; in-memory storage and lexical backend, not live HTTP or provider')))


if __name__ == '__main__':
    main()
