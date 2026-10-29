"""Rebuild dependency-bound semantic manifests from explicitly verified saved capsules.

The optional transport compatibility bridge admits only a remote.py source hash
change. Model, formatting, dimension, normalization and every other dependency
must match. Old capsules remain immutable; provenance records the bridge.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb_platform.core import Actor, Platform  # noqa: E402
from kb_platform.google_semantic_provider import GoogleSemanticProvider, write_google_page_capsule  # noqa: E402
from kb_platform.rag_testing.budget import BudgetLedger  # noqa: E402
from kb_platform.rag_testing.contracts import digest  # noqa: E402
from kb_platform.rag_testing.runner import write_json  # noqa: E402
from kb_platform.semantic_pages_v2 import SemanticPageService  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_saved_bindings(value, current):
    expected = [dict(page_index=p['page_index'], start=p['start'], end=p['end'],
                     text_sha256=hashlib.sha256(p['text'].encode()).hexdigest()) for p in current['pages']]
    if value.get('source') != current['source_identity'] or value.get('pages') != expected:
        raise ValueError('Saved vectors do not bind the current source version and exact page text')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--capsules', type=Path, required=True)
    parser.add_argument('--previous-remote-sha256')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.runtime.read_text('utf-8'))
    old = json.loads(args.manifest.read_text('utf-8'))
    ledger = BudgetLedger(Path(config['ledger_path']), config['caps'])
    before = ledger.summary()
    provider = GoogleSemanticProvider(config['provider_config'], ledger=ledger, audit_dir=config['audit_dir'])
    old_model, new_model = deepcopy(old['embedding']), deepcopy(provider.manifest)
    bridge = old_model != new_model
    if bridge:
        previous = old_model['implementation_sha256'].pop('remote.py')
        current = new_model['implementation_sha256'].pop('remote.py')
        if previous != args.previous_remote_sha256 or old_model != new_model:
            raise ValueError('Only explicitly reviewed transport-source compatibility is admitted')
    actor = Actor(old['tenant_id'], old['built_by_principal'])
    platform = Platform(os.environ['KB_DSN'], '', args.output / 'backup-only', search_backend='postgres')
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
            if bridge:
                item = catalog[source]
                target = args.output / (source + '.capsule.json')
                write_google_page_capsule(target, source_identity=item['source_identity'], pages=item['pages'],
                    source_text=item['source_text'], vectors=value['vectors'], embedding_manifest=provider.manifest,
                    provenance={'original': value['provenance'], 'original_capsule_sha256': sha(path),
                        'compatibility': 'reviewed transport Retry-After exception metadata only',
                        'old_remote_sha256': previous, 'new_remote_sha256': current}, authorize=snapshot['authorize'])
                path = target
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
        transport_compatibility_bridge=bridge, new_embedding_calls=0, ledger_unchanged=True,
        runtime=str((args.output / 'runtime.json').resolve())))
    provider.close()
    platform.backend.close()
    print(json.dumps({'status': 'PASS', 'new_embedding_calls': 0, 'ledger_unchanged': True}))


if __name__ == '__main__':
    main()
