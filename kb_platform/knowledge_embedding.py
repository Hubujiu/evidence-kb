from hashlib import sha256


def embedding_audit(actor, kb_id, build_id, authority_revision, rows, *, purpose='llm_context'):
    return {'tenant_id': actor.tenant_id, 'kb_id': kb_id, 'build_id': build_id,
            'principal': actor.principal_id, 'acl_revision': str(authority_revision), 'purpose': purpose,
            'sources': [{'source_id': row['source_id'], 'source_version_id': row['source_version_id'],
                'parse_snapshot_id': row['parse_snapshot_id'],
                'source_text_sha256': sha256('\n'.join(element['raw_text']
                    for element in row['document']['elements']).encode()).hexdigest()} for row in rows]}
