import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb_platform.core import Actor
from kb_platform.application_composition import create_platform
from kb_platform.settings import Settings
from kb_platform.google_semantic_provider import GoogleSemanticProvider
from kb_platform.rag_testing.budget import BudgetLedger
from kb_platform.rag_testing.contracts import digest
from kb_platform.rag_testing.runner import write_json
from kb_platform.semantic_pages_v2 import SemanticPageService


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_saved_bindings(value, current):
    expected = [dict(page_index=p['page_index'], start=p['start'], end=p['end'],
                     text_sha256=hashlib.sha256(p['text'].encode()).hexdigest()) for p in current['pages']]
    if value.get('source') != current['source_identity'] or value.get('pages') != expected:
        raise ValueError('Saved vectors do not bind the current source version and exact page text')


def main():
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--capsules', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.runtime.read_text('utf-8'))
    old = json.loads(args.manifest.read_text('utf-8'))
    ledger = BudgetLedger(Path(config['ledger_path']), config['caps'])
    before = ledger.summary()
    provider = GoogleSemanticProvider(config['provider_config'], ledger=ledger, audit_dir=config['audit_dir'])
    if old['embedding'] != provider.manifest:
        raise ValueError('Saved vectors require the current embedding manifest')
    actor = Actor(old['tenant_id'], old['built_by_principal'])
    platform = create_platform(Settings(os.environ['KB_DSN'], '', args.output / 'backup-only',
                                        args.output / 'unused-identities', 'postgres'))
    service = SemanticPageService(platform, provider, args.output / 'semantic-manifests')
    imported = {}
    with service.authority.locked(actor, old['kb_id'], manage=True) as snapshot:
        selected = {r['source_id'] for r in old['capsule_imports']}
        rows = [r for r in snapshot['rows'] if r['source_id'] in selected]
        catalog = service.authority.catalog(actor, old['kb_id'], snapshot, rows)
        for receipt in old['capsule_imports']:
            source = receipt['source_id']
            path = args.capsules / (source + '.capsule.json')
            if sha(path) != receipt['capsule_sha256']:
                raise ValueError('Original capsule hash differs')
            value = json.loads(path.read_text('utf-8'))
            checksum = value.pop('content_sha256')
            if digest(value) != checksum or value['embedding_manifest'] != old['embedding']:
                raise ValueError('Original capsule content or model differs')
            validate_saved_bindings(value, catalog[source])
            imported[source] = {'path': str(path.resolve()), 'sha256': sha(path)}
    with patch.object(provider.remote, '_post', side_effect=AssertionError('Migration must not call embeddings')):
        result = service.build(actor, old['kb_id'], imported)
        observed, _ = service.current_manifest(actor, old['kb_id'])
        service._index_bound(observed, result['manifest_sha256'])
    if ledger.summary() != before:
        raise ValueError('Migration changed provider budget ledger')
    runtime = {**config, 'storage_root': str(service.root), 'expected_provider_manifest_sha256': digest(provider.manifest)}
    write_json(args.output / 'runtime.json', runtime)
    write_json(args.output / 'receipt.json', dict(status='PASS', tenant_id=actor.tenant_id, kb_id=old['kb_id'],
        manifest_sha256=result['manifest_sha256'], original_manifest_sha256=sha(args.manifest),
        new_embedding_calls=0, ledger_unchanged=True,
        runtime=str((args.output / 'runtime.json').resolve())))
    provider.close()
    platform.backend.close()
    print(json.dumps({'status': 'PASS', 'new_embedding_calls': 0, 'ledger_unchanged': True}))


if __name__ == '__main__':
    main()
