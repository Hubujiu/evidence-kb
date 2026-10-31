import argparse
import json
import os
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.core import Platform
from kb_platform.delivery_units import indexed_elements
from kb_platform.documents import parse_native_document
from kb_platform.rag_testing.contracts import Plan
from kb_platform.rag_testing.delivery_fixtures import create_delivery_fixtures, load_delivery_fixtures
from kb_platform.rag_testing.delivery_http import execute_prepared, prepare_http, rebind_http
from kb_platform.settings import Settings


def estimate(fixtures):
    result = {}
    for split in ('dev', 'holdout'):
        documents = [doc for doc in fixtures['documents'] if doc['split'] == split]
        units, failures = [], []
        for doc in documents:
            raw = (Path(fixtures['root']) / doc['relative_path']).read_bytes()
            parsed = parse_native_document(raw, doc['filename'])
            if parsed['status'] != 'READY':
                failures.append({'document_id': doc['id'], 'status': parsed['status']})
            units.extend(indexed_elements(parsed, include_pages=True))
        texts = [unit['raw_text'] for unit in units if unit['kind'] != 'Page']
        result[split] = {'documents': len(documents), 'cases': 12, 'indexed_units': len(units),
            'embedding_texts': len(texts), 'embedding_characters': sum(map(len, texts)),
            'max_embedding_text_characters': max(map(len, texts), default=0), 'parse_failures': failures,
            'embedding_requests': 'provider_batching_dependent; not_inferred_from_text_count', 'remote_calls': 0}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare original-file HTTP fixtures or execute the existing unified RAG runner.')
    sub = parser.add_subparsers(dest='command', required=True)
    fixture_parser = sub.add_parser('fixtures', help='Generate new deterministic originals and frozen case annotations')
    fixture_parser.add_argument('--output', type=Path, required=True)
    estimate_parser = sub.add_parser('estimate', help='Offline parse/index workload; no model or upload')
    estimate_parser.add_argument('--fixtures', type=Path, required=True)
    prepare = sub.add_parser('prepare', help='Explicit real HTTP upload/build/publish; may call configured embedding')
    prepare.add_argument('--fixtures', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--expected-tenant', required=True)
    prepare.add_argument('--storage-root', type=Path, required=True)
    prepare.add_argument('--context-budget', type=int, default=118784, help='Frozen scorer evidence JSON budget, in codepoints')
    rebind = sub.add_parser('rebind', help='Read-only reuse of completed receipts into a new live-authority binding')
    rebind.add_argument('--prepared', type=Path, required=True)
    rebind.add_argument('--output', type=Path, required=True)
    rebind.add_argument('--expected-tenant', required=True)
    rebind.add_argument('--storage-root', type=Path, required=True)
    execute = sub.add_parser('execute', help='Explicit real HTTP turns through unified runner.run')
    execute.add_argument('--prepared', type=Path, required=True)
    execute.add_argument('--output', type=Path, required=True)
    execute.add_argument('--config', choices=('evidence_release_candidate',), required=True)
    execute.add_argument('--split', choices=('dev', 'holdout'))
    execute.add_argument('--required-stage', action='append', default=[])
    execute.add_argument('--plan', type=Path)
    execute.add_argument('--case-id', action='append', help='Freeze an explicit previously seen engineering regression subset')
    for child in (prepare, rebind, execute):
        child.add_argument('--base-url', required=True)
        child.add_argument('--token-env', default='KB_DELIVERY_API_TOKEN')
        child.add_argument('--physical-records', type=Path, required=True)
        child.add_argument('--timeout', type=float, default=1900)
    args = parser.parse_args(argv)
    if args.command == 'fixtures':
        data = create_delivery_fixtures(args.output)
        print(json.dumps({'root': data['root'], 'documents': len(data['documents']), 'cases': len(data['cases'])}))
        return 0
    if args.command == 'estimate':
        print(json.dumps(estimate(load_delivery_fixtures(args.fixtures)), ensure_ascii=False))
        return 0
    token = os.environ.get(args.token_env)
    if not token:
        parser.error('Set the explicitly selected token environment variable; credentials are not accepted in command arguments')
    with httpx.Client(base_url=args.base_url, headers={'Authorization': 'Bearer ' + token},
                      timeout=args.timeout, trust_env=False) as client:
        if args.command in ('prepare', 'rebind'):
            settings = Settings.from_environment()
            platform = Platform(settings.dsn, settings.search_url, args.storage_root)
            if args.command == 'rebind':
                prepared = rebind_http(client, args.prepared, args.output, platform=platform,
                    expected_tenant_id=args.expected_tenant, physical_directory=args.physical_records)
            else:
                prepared = prepare_http(client, args.fixtures, args.output, platform=platform,
                    expected_tenant_id=args.expected_tenant, physical_directory=args.physical_records,
                    context_budget=args.context_budget)
            print(json.dumps({'status': 'COMPLETE', 'scopes': {split: value['scope_id']
                for split, value in prepared['splits'].items()}, 'planned_cases': len(prepared['planned_case_ids']),
                'authority': {split: value['authority'] for split, value in prepared['splits'].items()},
                'reused_http_operation_receipts': prepared.get('reused_http_operation_receipts', 0),
                'new_upload_build_publish_requests': prepared.get('new_upload_build_publish_requests')}))
            return 0
        report = execute_prepared(client, args.prepared, args.output, config_name=args.config,
            physical_directory=args.physical_records, split=args.split, required_stages=args.required_stage,
            plan=Plan.model_validate_json(args.plan.read_text('utf-8')) if args.plan else None, case_ids=args.case_id)
        print(json.dumps({key: report[key] for key in ('decision', 'total', 'counts', 'mode')}, ensure_ascii=False))
        return {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2}[report['decision']]


if __name__ == '__main__':
    raise SystemExit(main())
