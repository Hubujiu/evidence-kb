import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.rag_testing.contracts import AdapterInput, Case, Plan, Suite, digest
from kb_platform.rag_testing.delivery_http import DeliveryHTTPAdapter, load_prepared
from kb_platform.rag_testing.runner import run, write_json


def freeze_conversations(prepared_directory, output):
    prepared_root, output = Path(prepared_directory).resolve(), Path(output).resolve()
    prepared = load_prepared(prepared_root)
    files = prepared['splits']['dev']['files']
    base = Suite.model_validate_json((prepared_root / files['suite']).read_text('utf-8'))
    documents = json.loads((prepared_root / files['documents']).read_text('utf-8'))
    by_case = {case.input.case_id: case for case in base.cases}
    pptx_case, txt_case = by_case['dev-pptx'], by_case['dev-txt']
    contact_source, first_source = documents['dev-pptx']['source_id'], documents['dev-txt']['source_id']
    common = pptx_case.input.model_dump(exclude={'case_id', 'question', 'history', 'allowed_sources'})
    correction = Case(input=AdapterInput(**common, case_id='conversation-correction-520',
        question='请汇总我最后确认的预算，以及资料中的值班热线和服务时段。预算是我提供的信息，不能说成资料规定。',
        allowed_sources=pptx_case.input.allowed_sources, history=(
            {'message': '我这次培训预算520元，请记住。先查澄川值班热线。'},
            {'message': '更正预算：以680元为准，520元作废。这里只记录我的更正。'},
            {'message': '再次更正，最终预算为520元，680元不再适用。请再核对那份值班资料的服务时段。'})),
        family='delivery-real-session-budget-correction', split='dev',
        slices=('actual_http_multiturn', 'user_correction', 'source_vs_user_statement'),
        any_of=pptx_case.any_of, qrels=pptx_case.qrels)
    switch = Case(input=AdapterInput(**common, case_id='conversation-explicit-locked-source-switch',
        question='请仅根据当前锁定的值班资料，告诉我热线及服务时段，不要带入前面的门禁规则。',
        allowed_sources=(contact_source,), history=(
            {'message': '仅使用这份身份登记规则，说明澄川 E417 的处理条件。',
             'source_policy': {'mode': 'LOCKED', 'source_ids': [first_source]}},
            {'message': '现在明确切换到值班联系资料，解除前面的来源锁，只按新的来源回答热线。',
             'source_policy': {'mode': 'LOCKED', 'source_ids': [contact_source]}})),
        family='delivery-real-session-explicit-source-switch', split='dev',
        slices=('actual_http_multiturn', 'source_policy_switch'), any_of=pptx_case.any_of,
        qrels={contact_source: 1}, forbidden_text=tuple(
            evidence.quote for evidence in base.evidence if any(evidence.id in group for group in txt_case.any_of)))
    suite = Suite(id='delivery-conversations-actual-http/1', label_provenance='synthetic_engineering',
                  sources=base.sources, evidence=base.evidence, cases=(correction, switch))
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'suite.json', suite.model_dump(mode='json'))
    write_json(output / 'plan.json', Plan(suite_hash=digest(suite), mode='live', required_stages=('assembly',),
                                        require_answer_behavior=True).model_dump(mode='json'))
    write_json(output / 'product-inputs.json', {case.input.case_id: {
        'history': list(case.input.history), 'question': case.input.question,
        'actual_user_turn_count': len(case.input.history) + 1} for case in suite.cases})
    write_json(output / 'review-expectations.json', {
        correction.input.case_id: {'reference': '最后确认的预算是520元且明确来自用户；热线和服务时段来自PPT原件。',
                                   'semantic_verification': 'independent_review_required'},
        switch.input.case_id: {'reference': '仅引用当前PPT值班资料，不将旧TXT门禁规则作为当前依据。',
                              'semantic_verification': 'independent_review_required'}})
    write_json(output / 'manifest.json', {'prepared_root': str(prepared_root),
        'prepared_manifest_sha256': sha256((prepared_root / 'prepared.json').read_bytes()).hexdigest(),
        'binding_file': str(prepared_root / files['bindings']), 'suite_hash': digest(suite),
        'cases': 2, 'actual_user_turns': 7, 'idempotent_final_http_replay_per_case': True,
        'claim_boundary': 'short_real_session_challenges; not_a_128K_saturated_conversation_test'})
    return suite


def execute_conversations(client, frozen_directory, output, *, config_name, physical_directory):
    root, output = Path(frozen_directory).resolve(), Path(output)
    manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
    prepared_root = Path(manifest['prepared_root'])
    if sha256((prepared_root / 'prepared.json').read_bytes()).hexdigest() != manifest['prepared_manifest_sha256']:
        raise ValueError('CONVERSATION_PREPARED_MANIFEST_CHANGED')
    load_prepared(prepared_root)
    suite = Suite.model_validate_json((root / 'suite.json').read_text('utf-8'))
    plan = Plan.model_validate_json((root / 'plan.json').read_text('utf-8'))
    if digest(suite) != manifest['suite_hash'] or len(suite.cases) != manifest['cases']:
        raise ValueError('CONVERSATION_FROZEN_SUITE_CHANGED')
    if output.exists():
        raise FileExistsError(output)
    bindings = json.loads(Path(manifest['binding_file']).read_text('utf-8'))
    adapter = DeliveryHTTPAdapter(client, config_name=config_name, bindings=bindings,
        physical_directory=physical_directory, artifacts=output.with_name(output.name + '-artifacts'),
        verify_idempotent_replay=True)
    return run(suite, plan, output, adapter=adapter)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Freeze/execute two real-session conversation challenges with the unified runner.\n\nSynthetic engineering cases only. The scorer checks the existing evidence and\nbehavior contracts; answer/memory semantics remain visible in raw sidecars for\nindependent review, never replaced with a product verifier score.\n')
    sub = parser.add_subparsers(dest='command', required=True)
    freeze = sub.add_parser('freeze')
    freeze.add_argument('--prepared', type=Path, required=True)
    freeze.add_argument('--output', type=Path, required=True)
    execute = sub.add_parser('execute')
    execute.add_argument('--frozen', type=Path, required=True)
    execute.add_argument('--output', type=Path, required=True)
    execute.add_argument('--base-url', required=True)
    execute.add_argument('--config', choices=('evidence_release_candidate',), required=True)
    execute.add_argument('--physical-records', type=Path, required=True)
    execute.add_argument('--token-env', default='KB_DELIVERY_API_TOKEN')
    args = parser.parse_args(argv)
    if args.command == 'freeze':
        suite = freeze_conversations(args.prepared, args.output)
        print(json.dumps({'cases': len(suite.cases), 'actual_user_turns': 7, 'remote_calls': 0}))
        return 0
    token = os.getenv(args.token_env)
    if not token:
        parser.error('Set the selected API token environment variable')
    with httpx.Client(base_url=args.base_url, headers={'Authorization': 'Bearer ' + token},
                      timeout=1900, trust_env=False) as client:
        report = execute_conversations(client, args.frozen, args.output, config_name=args.config,
                                       physical_directory=args.physical_records)
    print(json.dumps({key: report[key] for key in ('decision', 'total', 'counts')}))
    return {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2}[report['decision']]


if __name__ == '__main__':
    raise SystemExit(main())
