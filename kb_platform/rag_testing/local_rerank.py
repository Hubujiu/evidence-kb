"""Local reranker component probe over integrity-checked historical candidates.

Uses the shared runner/scorer/ledger. No gold reaches inference, no generated
answer or final request is invented. Consequently full acceptance remains FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import time

from .budget import BudgetLedger
from .cache import StageCache, StageKey
from .contracts import Plan, Suite, Trace, digest
from .runner import run, write_json
from .scoring import corpus_hash


MODELS = {
    'bge-base': ('BAAI/bge-reranker-base', '2cfc18c9415c912f9d8155881c133215df768a70'),
    'qwen3-06b': ('Qwen/Qwen3-Reranker-0.6B', 'e61197ed45024b0ed8a2d74b80b4d909f1255473'),
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def load_history(directory):
    manifest = read(directory / 'manifest.json')
    for name, expected in manifest.items():
        path = directory / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Historical integrity failure: ' + name)
    suite = Suite.model_validate(read(directory / 'suite.json'))
    traces = {t.case_id: t for t in
              (Trace.model_validate(read(p)) for p in sorted((directory / 'traces').glob('*.json')))}
    if set(traces) != {c.input.case_id for c in suite.cases}:
        raise ValueError('Historical case set mismatch')
    sources = {s.id: s for s in suite.sources}
    for case in suite.cases:
        t = traces[case.input.case_id]
        if (t.corpus_hash != corpus_hash(suite) or t.principal != case.input.principal or
                t.acl_revision != case.input.acl_revision):
            raise ValueError('Historical identity mismatch')
        for f in t.stages['retrieval']:
            s = sources[f.source_id]
            if (f.source_id not in case.input.allowed_sources or f.kind != 'extract' or
                    f.kb != case.input.kb or f.version != s.version or
                    f.source_sha256 != s.sha256 or s.text[f.start:f.end] != f.text):
                raise ValueError('Unauthorized or corrupt historical fragment')
    return suite, traces, manifest


class LocalRanker:
    def __init__(self, name, ledger, output, device='cpu'):
        import torch
        import transformers
        from transformers import AutoModelForSequenceClassification, AutoModelForCausalLM, AutoTokenizer

        self.torch, self.name, self.ledger, self.output = torch, name, ledger, output
        self.device = device
        torch.set_num_threads(8)
        model_id, revision = MODELS[name]
        self.manifest = dict(model=model_id, revision=revision, batch_size=4,
                             max_length=512 if name == 'bge-base' else 2048,
                             dtype='float32' if device == 'cpu' else 'float16', device=device,
                             threads=8, truncation='reject', under_test=True,
                             torch_version=torch.__version__, transformers_version=transformers.__version__)
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, revision=revision, local_files_only=True,
                                                       padding_side='left' if name.startswith('qwen') else 'right')
        factory = AutoModelForCausalLM if name.startswith('qwen') else AutoModelForSequenceClassification
        self.model = factory.from_pretrained(model_id, revision=revision, local_files_only=True,
                                            torch_dtype=torch.float32 if device == 'cpu' else torch.float16).to(device).eval()
        self.prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query '
                       'and the Instruct provided. Note that the answer can only be "yes" or "no".'
                       '<|im_end|>\n<|im_start|>user\n')
        self.suffix = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'

    def rank(self, request, candidates):
        torch, tok = self.torch, self.tokenizer
        if self.name.startswith('qwen'):
            texts = [self.prefix + '<Instruct>: Given a web search query, retrieve relevant passages that answer '
                     f'the query\n<Query>: {request.question}\n<Document>: {f.text}' + self.suffix for f in candidates]
            encoded = tok(texts, padding=False, truncation=False, add_special_tokens=False)
        else:
            encoded = tok([(request.question, f.text) for f in candidates], padding=False, truncation=False)
        lengths = [len(x) for x in encoded['input_ids']]
        if max(lengths, default=0) > self.manifest['max_length']:
            raise ValueError(f'Input exceeds frozen model limit: {max(lengths)}; no silent truncation')
        scores, batch_records = [], []
        started = time.perf_counter()
        for offset in range(0, len(candidates), 4):
            batch = tok.pad({k: v[offset:offset + 4] for k, v in encoded.items()},
                            padding=True, return_tensors='pt').to(self.device)
            physical_id = digest([str(self.output.parent.resolve()), self.name, request.case_id, offset, request.model_dump(),
                                  [f.model_dump() for f in candidates], self.manifest])
            resources = {'local_batches': 1, 'pairs': len(batch['input_ids']), 'remote_calls': 0}
            self.ledger.reserve(physical_id, resources, category='local_rerank', require_settled=True)
            try:
                with torch.inference_mode():
                    if self.name.startswith('qwen'):
                        logits = self.model(**batch, logits_to_keep=1).logits[:, -1, :].float()
                        values = torch.softmax(logits[:, [tok.convert_tokens_to_ids('no'),
                                                         tok.convert_tokens_to_ids('yes')]], dim=1)[:, 1]
                    else:
                        values = self.model(**batch).logits.view(-1).float().sigmoid()
                    values = values.cpu().tolist()
                if len(values) != resources['pairs'] or not all(math.isfinite(v) for v in values):
                    raise ValueError('Invalid model scores')
                record = dict(physical_id=physical_id, offset=offset, scores=values,
                              input_tokens=sum(lengths[offset:offset + 4]))
                write_json(self.output / (physical_id + '.json'), record)
                self.ledger.finish(physical_id, status='COMPLETE', usage=resources)
                batch_records.append(record)
                scores.extend(values)
            except Exception as exc:
                self.ledger.finish(physical_id, status='ERROR', diagnostic=str(exc))
                raise
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        return order, dict(scores=scores, batches=batch_records, elapsed_ms=(time.perf_counter()-started)*1000,
                           tokens=sum(lengths), max_pair_tokens=max(lengths, default=0))


class HistoricalCandidateAdapter:
    def __init__(self, candidates, ranker, output):
        self.candidates, self.ranker, self.output = candidates, ranker, output
        self.cache = StageCache(output / 'stage-cache')
        self.implementation = 'historical-candidate-order/1' if ranker is None else 'local-rerank/1:' + ranker.name

    def run(self, request, sources, corpus):
        candidates = self.candidates[request.case_id]
        if not candidates:
            return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
                         principal=request.principal, acl_revision=request.acl_revision, status='NOT_RUN',
                         error='No recorded candidate pool; upstream error is retained, not synthesized')
        allowed = {s.id: s for s in sources}
        for f in candidates:
            s = allowed[f.source_id]
            if s.sha256 != f.source_sha256 or s.text[f.start:f.end] != f.text:
                raise ValueError('Current local snapshot authorization/integrity failure')
        key = StageKey(stage='rerank', implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       upstream=(digest(request), digest([f.model_dump() for f in candidates])),
                       config_hash=digest(self.ranker.manifest if self.ranker else {'order': 'original'}),
                       corpus_hash=corpus, index_snapshot=digest([f.model_dump() for f in candidates]),
                       principal=request.principal, acl_revision=request.acl_revision,
                       model_revision=self.ranker.manifest['revision'] if self.ranker else 'none',
                       sample_id=self.output.parent.name + ':' + request.case_id)
        assert self.cache.get(key, authorize=lambda: True, mode='live', under_test=True) is None
        order, detail = (list(range(len(candidates))), dict(batches=[], elapsed_ms=0, tokens=0)) if self.ranker is None else self.ranker.rank(request, candidates)
        ranked = tuple(candidates[i] for i in order)
        self.cache.put(key, dict(order=order, **detail))
        write_json(self.output / (digest(request.case_id) + '.json'),
                   dict(case_id=request.case_id, request_hash=digest(request),
                        candidates_hash=digest([f.model_dump() for f in candidates]), order=order, **detail))
        print(self.implementation, request.case_id, 'OK', flush=True)
        # retrieval holds full ranked output for the shared source-level IR metric.
        # top-k stages are fragment counts, explicitly distinct from source-level k.
        return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
                     principal=request.principal, acl_revision=request.acl_revision,
                     stages={'candidate_pool': candidates, 'retrieval': ranked,
                             'rerank_top5': ranked[:5], 'rerank_top10': ranked[:10]},
                     capture='unobserved', elapsed_ms=detail['elapsed_ms'],
                     model_calls=len(detail['batches']), input_tokens=detail['tokens'], output_tokens=0)


def summarize(suite, report):
    cases = {c.input.case_id: c for c in suite.cases}
    answerable = [r for r in report['rows'] if cases[r['case_id']].any_of]
    result = dict(total=report['total'], answerable=len(answerable), full_acceptance=report['counts'])
    result['component_errors'] = [dict(case_id=r['case_id'], reasons=r['reasons']) for r in report['rows']
                                  if r['outcome'] in ('ERROR', 'NOT_RUN')]
    for stage in ('candidate_pool', 'rerank_top5', 'rerank_top10'):
        complete = []
        for row in answerable:
            observed = set(row.get('stage_evidence', {}).get(stage, []))
            if any(set(path) <= observed for path in cases[row['case_id']].any_of):
                complete.append(row['case_id'])
        result[stage] = dict(complete=len(complete), denominator=len(answerable), case_ids=complete)
    for metric in ('recall', 'mrr', 'ndcg_lower_bound'):
        rows = [r for r in report['rows'] if cases[r['case_id']].qrels]
        result[metric + '_source_at5'] = (None if any(r['outcome'] in ('ERROR', 'NOT_RUN') for r in rows) else
            sum((r.get('ir') or {}).get(metric) or 0 for r in rows)/len(rows))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', nargs='*', choices=MODELS, default=list(MODELS))
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    args = parser.parse_args()
    suite, traces, manifest = load_history(args.history)
    args.output.mkdir(parents=True, exist_ok=False)
    candidates = {k: v.stages['retrieval'] for k, v in traces.items()}
    max_pairs = sum(len(v) for v in candidates.values()) * len(args.models)
    max_batches = sum(math.ceil(len(v)/4) for v in candidates.values()) * len(args.models)
    ledger = BudgetLedger(args.output / 'ledger.sqlite3',
                          {'local_batches': max_batches, 'pairs': max_pairs, 'remote_calls': 0})
    write_json(args.output / 'experiment.json', dict(history=str(args.history.resolve()),
        historical_manifest_hash=digest(manifest), suite_hash=digest(suite), models={k: MODELS[k] for k in args.models},
        max_pairs=max_pairs, max_batches=max_batches, new_remote_calls=0, sample_count=len(suite.cases),
        nature='synthetic historical regression; actual local inference; no new domain training',
        boundary='historical ACL only; no current online ACL or final request or answer observed',
        selection='all cases from frozen suite; no gold provided to model',
        cache='rerank under test; always executed; no inference cache read',
        source_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    summary = {}
    for name in ['original-order', *args.models]:
        raw = args.output / (name + '-raw')
        raw.mkdir()
        ranker = LocalRanker(name, ledger, raw, args.device) if name != 'original-order' else None
        if ranker:
            write_json(raw / 'model.json', ranker.manifest)
        adapter = HistoricalCandidateAdapter(candidates, ranker, raw)
        report = run(suite, Plan(suite_hash=digest(suite), mode='live',
                     required_stages=('candidate_pool', 'retrieval', 'rerank_top5', 'rerank_top10')),
                     args.output / name, adapter=adapter)
        summary[name] = summarize(suite, report)
        write_json(args.output / 'summary.json', summary)
        write_json(args.output / 'budget.json', ledger.summary())
        if ranker:
            del adapter, ranker
            import gc
            import torch
            gc.collect()
            torch.cuda.empty_cache()
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
