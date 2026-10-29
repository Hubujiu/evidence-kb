"""Import selected published OpenSearch snapshots without changing authority or embedding vectors."""
import argparse
import json
import os
from pathlib import Path
import sys

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb_platform.embedded_search import EmbeddedSearch  # noqa: E402
from kb_platform.search import content_order  # noqa: E402
from kb_platform.rag_testing.contracts import digest  # noqa: E402
from kb_platform.rag_testing.runner import write_json  # noqa: E402
from scripts.snapshot_delivery import read_index, require_quiescent  # noqa: E402


def import_index(client, backend, index, output):
    snapshot = read_index(client, index)
    units = []
    for row in snapshot['documents']:
        unit = dict(row['source'])
        if unit.get('kind') == 'Page':
            unit['text'] = unit.pop('original_page_text')
            unit.pop('page_text', None)
            unit['heading_text'] = unit.pop('page_heading', '')
        unit.pop('binding_key', None)
        if unit['unit_id'] != row['id']:
            raise ValueError('UNIT_ID_MISMATCH')
        units.append(unit)
    dimension = snapshot['mapping'].get('properties', {}).get('vector', {}).get('dimension')
    write_json(output / (index + '.json'), snapshot)
    if backend._path(index).exists():
        backend.verify(index)
    else:
        backend.stage(index, units, dimension)
    observed = backend.read_units(index)
    if sorted(units, key=content_order) != observed:
        raise ValueError('EXACT_INDEX_READBACK_MISMATCH')
    return dict(index=index, units=len(units), dimension=dimension, snapshot_sha256=digest(snapshot),
                imported_units_sha256=digest(observed), exact_readback=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant', action='append', required=True)
    parser.add_argument('--dsn-env', default='KB_RECOVERY_ADMIN_DSN')
    parser.add_argument('--search-url', default='http://127.0.0.1:59200')
    parser.add_argument('--storage-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require_quiescent()
    args.output.mkdir(parents=True, exist_ok=False)
    with psycopg.connect(os.environ[args.dsn_env]) as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        indexes = [r[0] for r in connection.execute('''SELECT b.index_name FROM builds b JOIN knowledge_bases k
            ON k.tenant_id=b.tenant_id AND k.kb_id=b.kb_id AND k.manifest_id=b.build_id
            WHERE k.tenant_id=ANY(%s) ORDER BY b.index_name''', (args.tenant,))]
    backend = EmbeddedSearch(args.storage_root / '.search-indexes')
    results = []
    with httpx.Client(base_url=args.search_url, trust_env=False, timeout=90) as client:
        for index in indexes:
            results.append(import_index(client, backend, index, args.output))
    write_json(args.output / 'receipt.json', dict(indexes=results, new_embedding_calls=0, authority_changes=0,
        observation='exact immutable unit and original float-vector readback; semantic manifests require explicit capsule rebuild'))
    print(json.dumps(dict(indexes=len(results), units=sum(r['units'] for r in results), new_embedding_calls=0)))


if __name__ == '__main__':
    main()
