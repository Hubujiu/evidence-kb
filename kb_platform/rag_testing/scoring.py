"""Fail-closed original-span scoring. No LLM or remote dependencies."""
from __future__ import annotations

import math
import json
from .contracts import Case, Plan, Suite, Trace, digest


def corpus_hash(suite: Suite):
    return digest([s.model_dump(mode="json") for s in suite.sources])


def covered(evidence, fragments):
    """Union adjacent/overlapping verified extracts; never combine different sources."""
    intervals = sorted((f.start, f.end) for f in fragments
                       if f.kind == "extract" and f.source_id == evidence.source_id)
    cursor = evidence.start
    for start, end in intervals:
        if start > cursor:
            break
        if end > cursor:
            cursor = end
        if cursor >= evidence.end:
            return True
    return False


def score_case(suite: Suite, case: Case, trace: Trace | None, plan: Plan):
    result = {"case_id": case.input.case_id, "family": case.family, "slices": case.slices,
              "critical": case.critical, "outcome": "NOT_RUN", "reasons": [],
              "complete_evidence": None, "stage_evidence": {}, "missing_evidence": [],
              "first_loss_stage": None, "ir": None, "behavior_correct": None}
    if trace is None:
        result["reasons"] = ["missing_trace"]
        return result
    reasons = result["reasons"]
    result['observed_behavior'] = trace.behavior
    result['expected_behavior'] = case.expected_behavior
    result['behavior_correct'] = trace.behavior == case.expected_behavior if trace.behavior is not None else None
    if trace.status != "OK":
        result.update(outcome="ERROR" if trace.status != "NOT_RUN" else "NOT_RUN")
        reasons.append(trace.status + ":" + (trace.error or "no diagnostic"))
        return result
    if (trace.case_id != case.input.case_id or trace.corpus_hash != corpus_hash(suite)
            or trace.principal != case.input.principal or trace.acl_revision != case.input.acl_revision):
        reasons.append("trace_identity_mismatch")
    if trace.capture == "unobserved" or (plan.mode == "live" and trace.capture != "pre_send"):
        reasons.append("final_request_not_observed")
    if not trace.request_messages:
        reasons.append("missing_final_request")
    # JSON fields, including quotes/escapes, must not create false text mismatch.
    def strings(value):
        if isinstance(value, str):
            yield value
            if value.lstrip().startswith(("{", "[")):
                try:
                    yield from strings(json.loads(value))
                except (ValueError, RecursionError):
                    pass
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, (tuple, list)):
            for child in value:
                yield from strings(child)
    visible = list(strings(trace.request_messages))
    if trace.final_context is None:
        reasons.append("missing_final_context")
    elif not trace.request_evidence_bindings and not any(trace.final_context in text for text in visible):
        reasons.append("context_absent_from_final_request")
    context_strings = list(strings(trace.final_context or ""))
    if any(not any(f.text in text for text in context_strings) for f in trace.final):
        reasons.append("fragment_absent_from_final_request")
    # Every visible evidence text must have a source mapping. Do not accept an
    # authorized fragment beside untracked/unauthorized extra evidence.
    context = trace.final_context or ""
    tracked = context in ("".join(f.text for f in trace.final), "\n\n".join(f.text for f in trace.final))
    if not tracked:
        try:
            records = json.loads(context)
            if isinstance(records, list) and all(isinstance(r, dict) for r in records):
                visible_records = [r for r in records if r.get("text")]
                tracked = (len(visible_records) == len(trace.final) and all(
                    r.get("text") == f.text and (r.get("source_id") or r.get("document_id") or r.get("doc_id")) == f.source_id
                    for r, f in zip(visible_records, trace.final)))
        except ValueError:
            pass
    if not tracked:
        reasons.append("untracked_context_content")
    if trace.request_evidence_bindings:
        bound = set()
        for binding in trace.request_evidence_bindings:
            try:
                message = trace.request_messages[binding.message_index]
                if message.get('role') not in ('tool', 'user'):
                    raise ValueError('evidence must be original input/tool result')
                value = (json.loads(message['content']) if isinstance(message['content'], str)
                         else message['content'])
                for step in binding.json_path:
                    value = json.loads(value) if step == '$json' else value[step]
                if value != trace.final[binding.fragment_index].text:
                    raise ValueError('wire evidence differs from original fragment')
                bound.add(binding.fragment_index)
            except (IndexError, KeyError, TypeError, ValueError):
                reasons.append('fragment_absent_from_final_request:invalid_wire_binding')
        if bound != set(range(len(trace.final))):
            reasons.append('fragment_absent_from_final_request:incomplete_wire_bindings')
    # Budget is deliberately named codepoints, not a pretend tokenizer estimate.
    context_size = (len(json.dumps(trace.request_messages, ensure_ascii=False))
                    if trace.request_evidence_bindings else len(trace.final_context or ""))
    if context_size > case.input.context_budget:
        reasons.append("context_budget_exceeded")
    result["context_size"] = context_size
    if any(term in text for term in case.forbidden_text for text in visible):
        reasons.append("forbidden_text_in_request")
    sources = {s.id: s for s in suite.sources}
    evidence = {e.id: e for e in suite.evidence}
    needed = {e for group in case.any_of for e in group}
    all_stages = dict(trace.stages)
    all_stages["final"] = trace.final
    for name in plan.required_stages:
        if name not in all_stages:
            reasons.append("missing_stage:" + name)
    semantic_pending = False
    for name, fragments in all_stages.items():
        valid = []
        for f in fragments:
            s = sources.get(f.source_id)
            if s is None or f.source_id not in case.input.allowed_sources or f.kb != case.input.kb:
                reasons.append("unauthorized_source:" + name + ":" + f.source_id)
                continue
            if (f.version != s.version or f.source_sha256 != s.sha256 or f.kb != s.kb
                    or f.end > len(s.text) or f.start >= f.end):
                reasons.append("source_version_or_anchor_mismatch:" + name)
                continue
            if f.kind == "extract" and s.text[f.start:f.end] != f.text:
                reasons.append("original_text_mismatch:" + name)
                continue
            if f.kind == "paraphrase" and name == "final":
                semantic_pending = True
            valid.append(f)
        result["stage_evidence"][name] = sorted(e for e in needed if covered(evidence[e], valid))
    final = set(result["stage_evidence"]["final"])
    complete = any(set(group) <= final for group in case.any_of)
    result["complete_evidence"] = complete if case.any_of else None
    result["missing_evidence"] = sorted(needed - final)
    previous = None
    for name in (*plan.required_stages, "final"):
        hits = set(result["stage_evidence"].get(name, []))
        if previous is not None and previous - hits and result["first_loss_stage"] is None:
            result["first_loss_stage"] = name
        previous = hits
    retrieved = set(result["stage_evidence"].get("retrieval", []))
    result["evidence_retention"] = len(final & retrieved) / len(retrieved) if retrieved else None
    result["best_path_coverage"] = max((len(set(g) & final) / len(g) for g in case.any_of), default=None)
    if case.expected_behavior == "answer" and not complete:
        reasons.append("incomplete_final_evidence")
    if case.expected_behavior == 'answer' and plan.require_answer_behavior and trace.behavior != 'answer':
        reasons.append('answer_behavior_not_verified' if trace.behavior is None else 'answer_behavior_mismatch')
    if case.expected_behavior != "answer":
        result["behavior_correct"] = trace.behavior == case.expected_behavior
        if plan.objective != 'evidence' and trace.behavior != case.expected_behavior:
            reasons.append("behavior_not_verified")
    ranked = list(dict.fromkeys(f.source_id for f in trace.stages.get("retrieval", ())))[:plan.top_k]
    if case.qrels:
        positive = {s for s, grade in case.qrels.items() if grade > 0}
        gains = [case.qrels.get(s, 0) for s in ranked]
        dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))
        ideal = sorted(case.qrels.values(), reverse=True)[:plan.top_k]
        idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
        judged = all(s in case.qrels for s in ranked)
        result["ir"] = {"unit": "source", "k": plan.top_k,
                        "recall": len(set(ranked) & positive) / len(positive) if positive else None,
                        "mrr": next((1 / (i + 1) for i, s in enumerate(ranked) if s in positive), 0),
                        "ndcg_lower_bound": dcg / idcg if idcg else None,
                        "precision": len(set(ranked) & positive) / plan.top_k if judged else None,
                        "unjudged": [s for s in ranked if s not in case.qrels]}
    result["outcome"] = "FAIL" if reasons else "PASS"
    if semantic_pending and reasons == ["incomplete_final_evidence"]:
        result["outcome"] = "UNCERTAIN"
        reasons.append("semantic_review_required")
    return result


def evaluate(suite: Suite, traces: list[Trace], plan: Plan):
    if digest(suite) != plan.suite_hash:
        raise ValueError("suite differs from frozen plan")
    ids = [t.case_id for t in traces]
    expected = {c.input.case_id for c in suite.cases}
    if len(ids) != len(set(ids)) or not set(ids) <= expected:
        raise ValueError("duplicate or unexpected trace")
    by_id = {t.case_id: t for t in traces}
    rows = [score_case(suite, c, by_id.get(c.input.case_id), plan) for c in suite.cases]
    counts = {s: sum(r["outcome"] == s for r in rows)
              for s in ("PASS", "FAIL", "ERROR", "NOT_RUN", "UNCERTAIN")}
    slices = {}
    for name in sorted({s for c in suite.cases for s in c.slices} | plan.slice_floors.keys()):
        subset = [r for r in rows if name in r["slices"]]
        slices[name] = {"total": len(subset), "passed": sum(r["outcome"] == "PASS" for r in subset)}
    rate = counts["PASS"] / len(rows)
    blocked = any(counts[s] for s in ("ERROR", "NOT_RUN", "UNCERTAIN"))
    hard_prefixes = ("unauthorized_source", "source_version_or_anchor_mismatch", "original_text_mismatch",
                     "trace_identity_mismatch", "forbidden_text_in_request", "context_budget_exceeded",
                     "fragment_absent", "context_absent", "missing_final", "final_request_not_observed",
                     "missing_stage", "untracked_context_content")
    hard_failure = any(reason.startswith(hard_prefixes) for r in rows for reason in r["reasons"])
    failed = hard_failure or rate < plan.minimum_pass_rate or any(
        r["critical"] and r["outcome"] == "FAIL" for r in rows)
    for name, floor in plan.slice_floors.items():
        s = slices[name]
        blocked |= s["total"] < plan.minimum_slice_cases
        failed |= bool(s["total"] and s["passed"] / s["total"] < floor)
    answerable = [r for r, c in zip(rows, suite.cases) if c.expected_behavior == "answer"]
    answer_behavior = {
        'expected_answer_cases': len(answerable),
        'answered': sum(r.get('observed_behavior') == 'answer' and r['outcome'] not in ('ERROR', 'NOT_RUN') for r in answerable),
        'abstained': sum(r.get('observed_behavior') == 'abstain' and r['outcome'] not in ('ERROR', 'NOT_RUN') for r in answerable),
        'clarified_or_denied': sum(r.get('observed_behavior') in ('clarify', 'deny') and r['outcome'] not in ('ERROR', 'NOT_RUN') for r in answerable),
        'unobserved_or_error': sum(r.get('observed_behavior') is None or r['outcome'] in ('ERROR', 'NOT_RUN') for r in answerable),
        'required_for_pass': plan.require_answer_behavior,
    }
    return {"schema_version": "1", "plan_hash": digest(plan), "suite_hash": digest(suite),
            'objective': plan.objective,
            'unanswerable_case_scope': ('integrity/authorization/known forbidden content only; not sufficiency or generated refusal'
                if plan.objective == 'evidence' else 'source contract and observed answer behavior'),
            "mode": plan.mode, "decision": "FAIL" if failed else "BLOCKED" if blocked else "PASS",
            "total": len(rows), "counts": counts, "pass_rate": rate, "slices": slices,
            "final_complete_rate": sum(r["complete_evidence"] is True and r["outcome"] == "PASS"
                                       for r in answerable) / len(answerable)
            if answerable else None,
            "model_calls_recorded": sum(t.model_calls for t in traces),
            'answer_behavior': answer_behavior,
            "unknown_token_usage": sum(t.input_tokens is None or t.output_tokens is None for t in traces),
            "rows": rows}
