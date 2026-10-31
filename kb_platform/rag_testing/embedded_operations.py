import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from kb_platform.core import Actor, Platform, PlatformError
from .runner import write_json


def main():
    parser = argparse.ArgumentParser(description='Real PostgreSQL authority and local search operational checks; no RAG quality score.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', choices=['embedded', 'postgres'], default='embedded')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    dsn = os.environ['KB_DSN']
    root = args.output / 'originals'
    platform = Platform(dsn, '', root, search_backend=args.backend)
    actor = Actor('embedded-acceptance-' + uuid4().hex, 'owner')
    reader = Actor(actor.tenant_id, 'reader')
    scope = platform.create_kb(actor, 'Local search acceptance')['kb_id']
    passed = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        passed.append(name)
        write_json(args.output / 'operations.json', dict(passed=passed, tenant=actor.tenant_id, scope=scope, new_remote_calls=0))

    platform.set_member(actor, scope, reader.principal_id, ['kb.read'])
    source = platform.ingest(actor, scope, 'procedure.md', '# 规程\n\n旧版序号 ALPHA，权限收紧立即生效。'.encode(), readers=['reader'])
    build = platform.stage(actor, scope, ['bm25'])
    try:
        platform.search(reader, scope, 'ALPHA')
    except PlatformError as error:
        check('staging_is_not_published', error.code == 'NO_PUBLISHED_MANIFEST')
    else:
        check('staging_is_not_published', False)
    platform.publish(actor, scope, build['build_id'])
    check('publish_idempotent', platform.publish(actor, scope, build['build_id'])['idempotent'])
    result = platform.search(reader, scope, 'ALPHA')
    check('published_original_retrievable', bool(result['hits']))
    restarted = Platform(dsn, '', root, search_backend=args.backend)
    check('restart_same_citations', [h['citation_id'] for h in result['hits']] ==
          [h['citation_id'] for h in restarted.search(reader, scope, 'ALPHA')['hits']])
    platform.set_source_readers(actor, source['source_id'], [])
    check('revocation_immediate', not restarted.search(reader, scope, 'ALPHA')['hits'])
    platform.set_source_readers(actor, source['source_id'], ['reader'])
    platform.ingest(actor, scope, 'procedure.md', '# 规程\n\n新版序号 BETA，旧版已经废止。'.encode(),
                    readers=['reader'], source_id=source['source_id'])
    check('old_version_not_returned', not restarted.search(reader, scope, 'ALPHA')['hits'])
    build = platform.stage(actor, scope, ['bm25'])
    platform.set_source_readers(actor, source['source_id'], [])
    try:
        platform.publish(actor, scope, build['build_id'])
    except PlatformError as error:
        check('stale_publication_rejected', error.code == 'STALE_BUILD')
    else:
        check('stale_publication_rejected', False)
    build = platform.stage(actor, scope, ['bm25'])
    platform.publish(actor, scope, build['build_id'])
    check('new_version_retrievable', bool(platform.search(actor, scope, 'BETA')['hits']))
    check('revocation_survives_publication', not platform.search(reader, scope, 'BETA')['hits'])
    citation = platform.search(actor, scope, 'BETA')['hits'][0]['citation_id']
    resolved = platform.resolve_citation(actor, scope, citation)
    Path(resolved['original_path']).rename(Path(resolved['original_path']).with_suffix('.backup-offline'))
    check('missing_backup_does_not_break_evidence', bool(platform.resolve_citation(actor, scope, citation)['text']))
    try:
        platform.resolve_citation(actor, scope, citation, purpose='download')
    except PlatformError as error:
        check('missing_backup_download_fails_explicitly', error.code == 'SOURCE_INTEGRITY')
    else:
        check('missing_backup_download_fails_explicitly', False)
    print(json.dumps(dict(status='PASS', checks=len(passed), new_remote_calls=0)))


if __name__ == '__main__':
    main()
