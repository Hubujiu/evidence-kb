import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.rag_testing.contracts import AdapterInput, Case, Evidence, Fragment, Plan, Source, Suite, Trace, digest, text_hash
from kb_platform.rag_testing.scoring import corpus_hash
from kb_platform.rag_testing.runner import run, write_json
from kb_platform.rag_testing.capture import prepared_trace
from kb_platform.agent_context import ContextManager
from kb_platform.rag_testing.comparison import compare

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text('utf-8'))


def load_calls(base):
    calls = []
    for path in sorted(base.rglob('ledger.sqlite3')):
        if path.parent.name != 'calls':
            continue
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            for key, req, res, usage in db.execute(
                    "SELECT key,request_json,result_json,usage_json FROM jobs WHERE status='COMPLETE'"):
                req = json.loads(req)
                if req.get('purpose') != 'hard_answer':
                    continue
                result = json.loads(res)
                for message in req['payload']['messages']:
                    if message['role'] == 'user':
                        try:
                            content = json.loads(message['content'])
                        except ValueError:
                            continue
                        if 'passages' in content:
                            calls.append(dict(key=key, request=req, result=result, content=content,
                                              usage=json.loads(usage) if usage else {}, ledger=str(path)))
    return calls


def migrate(base, legacy, output):
    docs, questions, units = (read(base / name) for name in ('sources.json', 'questions.json', 'units.json'))
    rows = read(legacy / 'result-navigation-repaired.json')['rows']
    reviews = {(r['method'], r['qid']): r for r in read(legacy / 'review-result.json')['rows']}
    if len({(r['method'], r['question_id']) for r in rows}) != len(rows):
        raise ValueError('duplicate method/question rows')
    originals = {d['id']: d for d in docs}
    unit_map = {u['id']: u for u in units}
    sources = tuple(Source(id=d['id'], kb='historical-crossdoc', version=text_hash(d['text']),
                           text=d['text'], sha256=text_hash(d['text'])) for d in docs)
    source_map = {s.id: s for s in sources}
    evidence, cases = [], []
    for q in questions:
        path = []

        for docid in q['gold']:
            text = originals[docid]['text']
            offset = 0
            for para in text.split('\n\n'):
                quote = para.strip()
                if '§' in quote:
                    start = offset + para.index(quote)
                    eid = q['id'] + ':' + docid + ':' + str(start)
                    evidence.append(Evidence(id=eid, source_id=docid, start=start,
                                             end=start + len(quote), quote=quote, fact=quote))
                    path.append(eid)
                offset += len(para) + 2
        request = AdapterInput(case_id=q['id'], question=q['question'], kb='historical-crossdoc',
                               principal='historical-synthetic-actor', acl_revision='frozen-local-corpus',
                               allowed_sources=tuple(source_map), as_of='2026-10-24', context_budget=100000)
        cases.append(Case(input=request, family=q['group'], slices=(q['family'], q['tier']),
                          any_of=(tuple(path),) if q['answerable'] else (),
                          expected_behavior='answer' if q['answerable'] else 'abstain',
                          qrels={d: 1 for d in q['gold']}))
    suite = Suite(id='crossdoc-hard-existing-48-v1', label_provenance='synthetic_engineering',
                  sources=sources, evidence=tuple(evidence), cases=tuple(cases))
    plan = Plan(suite_hash=digest(suite), required_stages=('retrieval', 'assembly'))
    calls = load_calls(base)
    output.mkdir(parents=True, exist_ok=False)
    binding, summary, converted = [], {}, {}

    def fragment(p):
        s = source_map[p['doc_id']]
        return Fragment(source_id=s.id, source_sha256=s.sha256, version=s.version, kb=s.kb,
                        start=p['start'], end=p['end'], text=p['text'])

    for method in sorted({r['method'] for r in rows}):
        traces = []
        for row in [r for r in rows if r['method'] == method]:
            q = next(q for q in questions if q['id'] == row['question_id'])
            matches = [c for c in calls if c['request'].get('owner_scope', '').startswith(method + '/' + q['id'] + '/')
                       and c['content']['question'] == q['question']
                       and c['content']['passages'] == row['context']
                       and c['result'].get('value') == row['prediction']]
            matches.sort(key=lambda c: c['key'])
            call = matches[0] if matches else None
            final = tuple(fragment(p) for p in row['context'])
            context = json.dumps(row['context'], ensure_ascii=False)


            t = Trace(case_id=q['id'], corpus_hash=corpus_hash(suite), implementation='recorded:' + method,
                      principal='historical-synthetic-actor', acl_revision='frozen-local-corpus',
                      status='ERROR' if row['technical_error'] else 'OK',
                      error=str(row.get('reason')) if row['technical_error'] else None,
                      stages={'retrieval': tuple(fragment(unit_map[u]) for u in row['retrieved_units']),
                              'assembly': final}, final=final, final_context=context,
                      request_messages=tuple(call['request']['payload']['messages']) if call else (),
                      capture='replay' if call else 'unobserved',
                      behavior='abstain' if row['prediction']['abstain'] else 'answer',
                      input_tokens=call['usage'].get('prompt_tokens') if call else None,
                      output_tokens=call['usage'].get('completion_tokens') if call else None)
            traces.append(t)
            binding.append({'method': method, 'case_id': q['id'], 'matching_calls': len(matches),
                            'physical_key': call['key'] if call else None,
                            'request_hash': digest(call['request']['payload']) if call else None,
                            'legacy_score': row['score'], 'historical_tokens': row['evidence_tokens'],
                            'historical_4000_token_limit_ok': row['evidence_tokens'] <= 4000,
                            'tool_result': row.get('join_result')})
        report = run(suite, plan, output / method, traces=traces)
        converted[method] = traces
        old = [r for r in rows if r['method'] == method]
        matched = [r for r in report['rows'] if r['complete_evidence'] is True]
        summary[method] = {'total': len(old), 'legacy_answer_passed': sum(r['score'] for r in old),
                           'existing_semantic_answer_passed': sum(bool(reviews[(method, r['question_id'])].get('verdict', {}).get('reference_correct'))
                               for r in old if not reviews[(method, r['question_id'])]['technical_error']),
                           'framework_counts': report['counts'], 'complete_original_chains': len(matched),
                           'first_loss': dict(Counter(r['first_loss_stage'] for r in report['rows'])),
                           'reasons': dict(Counter(v for r in report['rows'] for v in r['reasons'])),
                           'answer_pass_but_incomplete_chain': [r['question_id'] for r in old if r['score']
                               and next(x for x in report['rows'] if x['case_id'] == r['question_id'])['complete_evidence'] is False],
                           'complete_chain_but_legacy_answer_failed': [r['question_id'] for r in old if not r['score']
                               and next(x for x in report['rows'] if x['case_id'] == r['question_id'])['complete_evidence'] is True]}


    class CurrentAssembly:
        implementation = 'current-ContextManager-over-recorded-hybrid'
        preserve_windows = False

        def run(self, request, authorized_sources, corpus):
            original = next(t for t in converted['hybrid'] if t.case_id == request.case_id)
            supplied = [{"document_id": f.source_id, "version": f.version,
                         "text": f.text if self.preserve_windows else source_map[f.source_id].text,
                         "context_offset": 0 if self.preserve_windows else f.start} for f in original.final]
            prepared = ContextManager().prepare('answer', 'Answer from supplied evidence.', {}, [],
                                                supplied, request.question)
            return prepared_trace(request=request, corpus_hash=corpus, implementation=self.implementation,
                                  prepared=prepared, sources=source_map, stages={'retrieval': original.stages['retrieval']},
                                  origin_offsets=tuple(f.start for f in original.final) if self.preserve_windows else None)

    answerable = suite.model_copy(update={'cases': tuple(c for c in suite.cases if c.expected_behavior == 'answer')})
    assembly = run(answerable, Plan(suite_hash=digest(answerable), mode='live', required_stages=('retrieval',)),
                   output / 'current-product-assembly', adapter=CurrentAssembly())
    candidate = CurrentAssembly()
    candidate.preserve_windows = True
    candidate.implementation = 'ContextManager-preserve-cached-original-windows'
    optimized = run(answerable, Plan(suite_hash=digest(answerable), mode='live', required_stages=('retrieval',)),
                    output / 'preserved-window-assembly', adapter=candidate)
    paired = compare(assembly, optimized)
    write_json(output / 'assembly-comparison.json', paired)
    audit = {'new_provider_calls': 0, 'historical_completed_answer_calls_available': len(calls),
             'input_hashes': {str(path): text_hash(path.read_text('utf-8')) for path in
                              [base / 'sources.json', base / 'questions.json', base / 'units.json',
                               legacy / 'result-navigation-repaired.json', legacy / 'review-result.json']},
             'rows': len(rows), 'requests_bound': sum(b['physical_key'] is not None for b in binding),
             'methods': summary, 'current_assembly_counts': assembly['counts'],
             'current_assembly_reasons': dict(Counter(v for r in assembly['rows'] for v in r['reasons'])),
             'preserved_window_counts': optimized['counts'],
             'assembly_paired': {k: v for k, v in paired.items() if k != 'pairs'},
             'limitations': ['Historical dataset is synthetic, not human business ground truth.',
                            'Actor/ACL are reconstructed corpus fixtures, not proof of historical authorization.',
                            'Framework codepoint guard is diagnostic; original 4000-token budget checked separately.',
                            'Complete source chain does not validate table calculation or final answer.',
                            'Current assembly uses recorded upstream results; no live retrieval or generation.']}
    write_json(output / 'request-bindings.json', binding)
    write_json(output / 'audit.json', audit)
    lines = ['# 既有 RAG 记录的统一框架重评', '',
             '复用了 168 份原文、48 题、五方案共 240 条真实执行记录；240/240 绑定到相同问题、片段及回答的已完成物理请求。新增模型与 embedding 调用均为 0。原数据是合成挑战集，其运行记录真实，但不属于真实企业人工业务金标。', '',
             '| 方案 | 原答案规则通过 /48 | 已有语义复核通过 /48 | 完整原文证据链 /40 有答案题 |',
             '|---|---:|---:|---:|']
    for method, values in summary.items():
        lines.append(f"| {method} | {values['legacy_answer_passed']:g} | {values['existing_semantic_answer_passed']} | {values['complete_original_chains']} |")
    lines += ['', '证据链与答案是不同指标，不能将框架 PASS 数增加解释为答题质量提升。无答案行为另计，未用新的裁判重评分。', '',
              '## 发现的问题', '',
              '- Wiki 的 Q8bb6e70bce 原答案规则通过，但 § 段落金标链不完整。应检查既有标注是否过强、是否存在替代证据或计算工具支持，暂不把它直接判作幻觉，也不自动修金标。',
              '- Agent 和 Graph 存在证据齐全但旧答案规则失败的题，详见 audit.json。框架将生成/评分问题与证据丢失分开，既有语义复核也已复用。',
              '- 多条记录在 retrieval 已出现标注证据，assembly 后缺失；逐题 first_loss_stage 和 missing_evidence 可定位。该字段检测任意金标单元损失，不代表每次都损失完整可回答性。',
              '- 历史身份与 ACL 没有充分原始凭证，本轮仅重建冻结本地语料身份；不能据此宣布真实权限验收通过。', '',
              '## 零调用优化对照', '',
              f"实际执行当前产品 ContextManager，对同一批 Hybrid 缓存的 40 道有答案题：原文窗口输入完整证据 {assembly['counts']['PASS']}/40；保留已选片段输入 {optimized['counts']['PASS']}/40。配对 {paired['wins']} 胜、{paired['ties']} 平、{paired['losses']} 负。",
              '本轮尝试未获得完整证据通过数提升。历史专用装配的 8/40 与当前产品装配的 4/40 使用不同装配协议和窗口限制，不能解释为产品线上退化；它揭示了接入协议不等价和剩余裁剪瓶颈。没有重新生成答案，因此没有新的答案质量或在线延迟收益结论。', '',
              '## 复现', '',
              '`.venv/Scripts/python.exe scripts/replay_rag_history.py --output reports/rag-history-v1/new-unique-run`', '',
              '脚本仅通过 SQLite mode=ro 读旧账本，通过 run SDK 接入产品 ContextManager，输出目录存在即拒绝覆盖。所有旧评分、请求和缓存保留。实际 provider 发送链的常驻采集未修改；本轮接入范围为离线请求回放和当前产品上下文组件。', '',
              '100000 codepoint 仅为迁移诊断护栏；历史 4000-token 条件在 request-bindings.json 中单独核验。规范化提高可复核性，目前没有实测答案质量提升。']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read-only historical RAG replay and current ContextManager integration; no provider imports.')
    parser.add_argument('--base', type=Path, default=ROOT / '.local/benchmark-crossdoc/crossdoc-hard-20261024-01')
    parser.add_argument('--legacy', type=Path, default=ROOT / 'reports/benchmark-crossdoc/crossdoc-hard-20261024-01')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.base, args.legacy, args.output), ensure_ascii=True, indent=2))
