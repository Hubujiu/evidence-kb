from kb_platform.rag_testing.llm_stream_lint import body, compact


def multiblock_source_violations(source, model):
    checks = {
        'atomic rejected event': ('pub fn push(', ['self.validate_snapshot()?;', 'letmutnext=self.clone();next.push_checked(event)?;*self=next;']),
        'indexed closed block admission': ('fn ensure(', ['self.blocks.iter().position(|p|p.index==index)', 'block.kind!=kind||block.closed.is_some()']),
        'unique executable tool identity': ('fn validate_finish(', ['!ids.insert(id)', 'id.is_empty()', 'name.is_empty()', 'v.is_object()', 'matches!(reason,FinishReason::ToolCalls)==ids.is_empty()']),
        'immutable block identity': ('fn push_checked(', ['self.blocks.iter().any(|p|p.index==index)', '(!block.id.is_empty()&&block.id!=id)', '(!active.id.is_empty()&&active.id!=*id)', 'ifself.finished()']),
        'replay positional projection': ('pub fn response(', ['blocks.len()!=kept.len()', '.zip(&kept)', 'keep.then_some(block.clone())', 'keep.then_some(block)', 'block.is_safe_text()']),
    }
    errors = []
    for label, (marker, required) in checks.items():
        actual = compact(body(source, marker))
        if not all(part in actual for part in required):
            errors.append(label)
    normalized = compact(model)
    if ('RejectedUnchanged==rejected=><<filled,kind,ids,valid,closed,order>>=rejectedBefore' not in normalized
            or "rejectedBefore'=<<filled,kind,ids,valid,closed,order>>" not in normalized
            or 'THEN[kindEXCEPT![i]=IF@="text"THEN"reasoning"ELSE"text"]' not in normalized):
        errors.append('rejection model must compare actual snapshots and mutate actual candidate fields')
    return errors
