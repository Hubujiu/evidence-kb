from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from kb_platform.agent_context import ContextManager, ContextPolicy
from .capture import prepared_trace
from .contracts import AdapterInput, Case, Evidence, Fragment, Source, Suite, digest, text_hash


class CanonicalRetentionAdapter:


    def __init__(self, snapshots, *, original_inputs, expected_corpus, retention,
                 packing, active_limit=16, input_budget=118784):
        if retention not in {'fifo', 'coverage'}:
            raise ValueError('UNKNOWN_RETENTION_POLICY')
        if type(active_limit) is not int or not 1 <= active_limit <= 50:
            raise ValueError('INVALID_ACTIVE_LIMIT')
        self.snapshots = deepcopy(snapshots)
        self.original_inputs = deepcopy(original_inputs)
        self.expected_corpus, self.retention = expected_corpus, retention
        self.active_limit = active_limit
        self.policy = ContextPolicy(input_budget=input_budget, output_reserve=4096,
            model_window=131072, safety_reserve=8192, evidence_strategy=packing)
        paths = [Path(__file__), Path(__file__).parents[1] / 'agent_context.py',
                 Path(__file__).parents[1] / 'evidence_selection.py',
                 Path(__file__).parents[1] / 'evidence_packing.py',
                 Path(__file__).parents[1] / 'prose_windows.py']
        self.implementation = digest({'code': {p.name: text_hash(p.read_text('utf-8')) for p in paths},
            'snapshots': self.snapshots, 'inputs': self.original_inputs, 'corpus': expected_corpus,
            'retention': retention, 'active_limit': active_limit, 'context_policy': self.policy.__dict__})
        self.observations = {}

    def run(self, request, sources, corpus):
        from kb_platform.agent_context import canonical
        from kb_platform.evidence_selection import select_evidence
        from .contracts import Trace

        if corpus != self.expected_corpus or request.model_dump(mode='json') != self.original_inputs[request.case_id]:
            raise ValueError('FROZEN_RETENTION_INPUT_CHANGED')
        snapshot = self.snapshots[request.case_id]
        allowed = {source.id: source for source in sources}
        catalog, fragments = {}, {}
        for cid, entry in snapshot['catalog'].items():
            fragment, row = Fragment.model_validate(entry['fragment']), deepcopy(entry['record'])
            source = allowed.get(fragment.source_id)
            if (source is None or row.get('citation_id') != cid or row.get('source_id') != fragment.source_id
                    or fragment.version != source.version or fragment.source_sha256 != source.sha256
                    or fragment.kb != source.kb or row.get('parse_text_start') != fragment.start
                    or row.get('parse_text_end') != fragment.end or row.get('text') != fragment.text
                    or source.text[fragment.start:fragment.end] != fragment.text):
                raise ValueError('FROZEN_RETENTION_ORIGINAL_MISMATCH')
            catalog[cid], fragments[cid] = row, fragment
        active, order, retrieved, offsets, steps = {}, [], {}, {}, []
        state = deepcopy(snapshot['state'])
        work = state.get('work_state', {})
        query = snapshot.get('selection_query') or snapshot['question']
        for batch in snapshot['batches']:
            ids = batch['ids']
            if any(cid not in catalog for cid in ids):
                raise ValueError('UNTRACKED_RETENTION_CITATION')
            for cid in ids:
                active[cid] = deepcopy(catalog[cid])
                retrieved[cid] = fragments[cid]
                order = [key for key in order if key != cid] + [cid]
            for cid, offset in batch.get('offsets', {}).items():
                if cid not in ids or type(offset) is not int or not 0 <= offset < len(catalog[cid]['text']):
                    raise ValueError('INVALID_FROZEN_READ_OFFSET')
                offsets[cid] = offset
            if self.retention == 'coverage':
                chosen = select_evidence(list(active.values()), self.active_limit, query,
                    subquestions=work.get('subquestions', ()),
                    preferred_ids=work.get('confirmed_evidence_ids', ()))
                active = {row['citation_id']: row for row in chosen}
            else:
                active = {cid: active[cid] for cid in order[-self.active_limit:] if cid in active}
            steps.append({'returned': list(ids), 'active': list(active)})
        evidence = [{**row, 'context_offset': offsets.get(cid, 0)} for cid, row in active.items()]
        prepared = ContextManager(self.policy).prepare('answer', snapshot['system'], state,
            deepcopy(snapshot['history']), evidence, snapshot['question'],
            deepcopy(snapshot.get('checkpoint')), selection_query=snapshot.get('selection_query'))
        final = []
        for row in prepared.projection['evidence']:
            if not row.get('text'):
                continue
            base = fragments[row['citation_id']]
            start = base.start + row['text_start']
            source = allowed[base.source_id]
            if start < base.start or start + len(row['text']) > base.end or source.text[start:start + len(row['text'])] != row['text']:
                raise ValueError('PACKED_RETENTION_ORIGINAL_MISMATCH')
            final.append(base.model_copy(update={'start': start, 'end': start + len(row['text']), 'text': row['text']}))
        self.observations[request.case_id] = {'steps': steps, 'active': list(active),
            'prepared': prepared.to_dict(), 'unique_recorded_candidates': len(retrieved),
            'execution_boundary': 'current_component_over_archived_upstream; no_provider_send',
            'new_model_embedding_judge_calls': 0}
        return Trace(case_id=request.case_id, corpus_hash=corpus, implementation=self.implementation,
            principal=request.principal, acl_revision=request.acl_revision,
            stages={'retrieval': tuple(retrieved.values()), 'assembly': tuple(final)}, final=tuple(final),
            final_context=canonical(prepared.projection['evidence']), request_messages=tuple(prepared.messages),
            capture='replay', model_calls=0, input_tokens=prepared.estimate['tokens'], output_tokens=0)


class ContextDecisionAdapter:
    def __init__(self, candidates, *, strategy, input_budget, original_inputs, expected_corpus):
        self.candidates = candidates
        self.original_inputs = original_inputs
        self.expected_corpus = expected_corpus
        self.policy = ContextPolicy(input_budget=input_budget, evidence_strategy=strategy)
        paths = [Path(__file__), Path(__file__).with_name("capture.py")]
        paths += [Path(__file__).parents[1] / name for name in ("agent_context.py", "evidence_packing.py")]
        self.implementation = digest({"code": {p.name: text_hash(p.read_text('utf-8')) for p in paths},
                                      "strategy": strategy, "input_budget": input_budget,
                                      "corpus": expected_corpus, "inputs": original_inputs,
                                      "ordered_candidates": {key: [f.model_dump(mode="json") for f in value]
                                                             for key, value in candidates.items()}})

    def run(self, request, sources, corpus):
        if corpus != self.expected_corpus:
            raise ValueError("cached retrieval corpus dependency changed")
        recorded = self.original_inputs[request.case_id]
        actual = request.model_dump(mode="json")

        if {k: v for k, v in actual.items() if k != "context_budget"} != {
                k: v for k, v in recorded.items() if k != "context_budget"}:
            raise ValueError("cached retrieval input dependency changed")
        allowed = {source.id: source for source in sources}
        supplied = self.candidates[request.case_id]
        if any(fragment.source_id not in allowed for fragment in supplied):
            raise ValueError("cached candidates outside current authorization")
        for fragment in supplied:
            source = allowed[fragment.source_id]
            if (fragment.version != source.version or fragment.source_sha256 != source.sha256 or
                    source.text[fragment.start:fragment.end] != fragment.text):
                raise ValueError("cached candidate identity or text changed")
        evidence = [{"document_id": f.source_id, "version": f.version, "text": f.text} for f in supplied]
        prepared = ContextManager(self.policy).prepare(
            "answer", "Answer from supplied evidence with exact quotations.", {}, list(request.history),
            evidence, request.question)
        trace = prepared_trace(request=request, corpus_hash=corpus, implementation=self.implementation,
                               prepared=prepared, sources=allowed,
                               stages={"retrieval": tuple(supplied)},
                               origin_offsets=tuple(f.start for f in supplied))
        return trace.model_copy(update={"stages": {**trace.stages, "assembly": trace.final}})


def synthetic_context_suite():

    sources, evidence, cases, candidates = [], [], [], {}
    facts = [
        ("negation", "设备红杉运行时是否允许开盖？", "设备红杉运行时不得开盖；仅在断电且等待十分钟后允许检修。"),
        ("table", "红杉设备2025年的费用和单位是什么？", "设备 | 年度 | 费用（万元）\n红杉 | 2025 | 42\n注：费用不含税。"),
        ("version", "红杉2025版标准的生效时间和例外是什么？", "红杉2025版标准自2025年7月1日生效；2024版标准只适用于已签署的旧合同。"),
        ("condition", "红杉备份的保留期限及适用条件是什么？", "红杉备份须保留90天；发生审计冻结时不得按期删除，解除冻结后才可清理。"),
    ]
    filler = "日常记录：检查照明、工位和包装材料，登记一般维护情况。\n\n"
    for family, question, fact in facts:
        for position in ("first", "middle", "last"):
            cid = f"context-{family}-{position}"
            left, right = {"first": (0, 90), "middle": (45, 45), "last": (90, 0)}[position]
            text = filler * left + fact + "\n\n" + filler * right
            source = Source(id=cid, kb="context-fixture", version="fixture-v1", text=text, sha256=text_hash(text))
            sources.append(source)
            start = text.index(fact)
            evidence.append(Evidence(id=cid + "-fact", source_id=cid, start=start, end=start + len(fact),
                                     quote=fact, fact=fact))
            request = AdapterInput(case_id=cid, question=question, kb=source.kb,
                                   principal="fixture-user", acl_revision="fixture-acl-v1",
                                   allowed_sources=(cid,), as_of="2026-10-25", context_budget=12000)
            cases.append(Case(input=request, family=family, slices=(family, position, "synthetic"),
                              any_of=((cid + "-fact",),), qrels={cid: 1}))
            candidates[cid] = (Fragment(source_id=cid, source_sha256=source.sha256, version=source.version,
                                        kb=source.kb, start=0, end=len(text), text=text),)
    return Suite(id="context-query-blocks-v1", label_provenance="synthetic_engineering",
                 sources=tuple(sources), evidence=tuple(evidence), cases=tuple(cases)), candidates
