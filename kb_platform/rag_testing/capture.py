from .contracts import Fragment, Trace
from kb_platform.agent_context import canonical


def prepared_trace(*, request, corpus_hash, implementation, prepared, sources, stages, origin_offsets=None):

    fragments = []
    if origin_offsets is not None and len(origin_offsets) != len(prepared.projection["evidence"]):
        raise ValueError("origin offsets must match all prepared evidence slots")
    for index, row in enumerate(prepared.projection["evidence"]):
        if not row.get("text"):
            continue
        source_id = row.get("source_id") or row.get("document_id")
        source = sources[source_id]
        start = row.get("text_start", row.get("offset", 0))
        if origin_offsets is not None:
            start += origin_offsets[index]
        fragments.append(Fragment(source_id=source.id, source_sha256=source.sha256,
                                  version=row.get("source_version_id", row.get("version", source.version)),
                                  kb=source.kb, start=start, end=start + len(row["text"]), text=row["text"]))
    return Trace(case_id=request.case_id, corpus_hash=corpus_hash, implementation=implementation,
                 principal=request.principal, acl_revision=request.acl_revision, stages=stages,
                 final=tuple(fragments), final_context=canonical(prepared.projection["evidence"]),
                 request_messages=tuple(prepared.messages), capture="pre_send")
