import base64
import copy
import hashlib
import io
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from ..documents import parse_native_document
from ..ocr import augment_ocr
from ..parsing_backends import routed_document


IDENTITY = {'packages': {'synthetic': '1'}, 'worker_sha256': 'synthetic-worker',
            'fixed_model_dependencies': True}


def pdf_fixture(pages=2):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for index in range(pages):
        page_id = len(objects) + 1
        kids.append(f'{page_id} 0 R')
        content = f'BT /F1 12 Tf 20 150 Td (Native page {index + 1}) Tj ET'.encode()
        objects.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>'.encode(),
                        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream'])
    objects[1] = f'<< /Type /Pages /Count {pages} /Kids [{" ".join(kids)}] >>'.encode()
    data, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def parsed_job(job, status='SUCCESS'):
    data = base64.b64decode(job['data'])
    return {'original_sha256': hashlib.sha256(data).hexdigest(),
            'records': [{'text': 'Synthetic external text', 'kind': 'Paragraph'}],
            'details': {'conversion_status': status}}


class PageRecoveryContracts(unittest.TestCase):
    def test_cached_partial_conversion_cannot_be_promoted_to_ready(self):
        data = pdf_fixture(1)
        policy = {'ocr': True, '_parse_authorization': {'principal': 'fixture', 'acl_revision': '1'}}
        native = parse_native_document(data, 'native.pdf', policy)
        self.assertEqual(native['status'], 'READY')
        calls = []

        def worker(payload, backend):
            if payload.get('mode') == 'identity':
                return dict(IDENTITY)
            calls.append(payload)
            return {'identity': dict(IDENTITY), 'results': [parsed_job(job, 'PARTIAL_SUCCESS') for job in payload['jobs']]}

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
                'EVIDENCE_KB_PARSE_ROUTE': 'docling', 'EVIDENCE_KB_PAGE_CACHE_ROOT': directory}), patch(
                'kb_platform.parsing_backends.external_worker', side_effect=worker):
            cold = routed_document(data, 'native.pdf', policy, copy.deepcopy(native))
            warm = routed_document(data, 'native.pdf', policy, copy.deepcopy(native))
        self.assertEqual(cold['status'], 'PARTIAL')
        self.assertEqual(warm['status'], 'PARTIAL')
        self.assertEqual(warm['quality_report']['parser_route']['cache_hits'], 1)
        self.assertEqual(warm['parse_snapshot_id'], cold['parse_snapshot_id'])
        self.assertEqual(len(calls), 1)

    def test_failed_fallback_does_not_prevent_later_page_recovery(self):
        data = pdf_fixture()
        policy = {'ocr': True}
        native = parse_native_document(data, 'native.pdf', policy)
        attempts = []

        def worker(payload, backend):
            if payload.get('mode') == 'identity':
                return dict(IDENTITY)
            if backend == 'docling_ocr':
                return {'identity': dict(IDENTITY), 'results': [
                    {'error': 'external parser returned no observable text'} for _ in payload['jobs']]}
            attempts.append(payload['jobs'][0])
            if len(attempts) == 1:
                return {'identity': dict(IDENTITY), 'results': [{'error': 'synthetic first-page OCR failure'}]}
            return {'identity': dict(IDENTITY), 'results': [parsed_job(payload['jobs'][0])]}

        with patch.dict(os.environ, {'EVIDENCE_KB_PARSE_ROUTE': 'docling_ocr'}), patch(
                'kb_platform.parsing_backends.external_worker', side_effect=worker):
            result = routed_document(data, 'native.pdf', policy, native)
        rows = result['quality_report']['parser_route']['pages']
        self.assertEqual(len(attempts), 2)
        self.assertEqual([row['status'] for row in rows], ['FAILED', 'COMPLETE'])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(rows[1]['attempts'][0]['status'], 'FAILED')
        self.assertEqual(rows[1]['attempts'][1]['status'], 'COMPLETE')
        self.assertEqual({anchor['page_index'] for anchor in result['anchors']}, {1})

    def test_explicit_ocr_recovery_does_not_repeat_successful_image_frames(self):
        stream = io.BytesIO()
        Image.new('RGB', (32, 32), 'white').save(stream, format='TIFF', save_all=True,
            append_images=[Image.new('RGB', (32, 32), 'gray')])
        data = stream.getvalue()
        native = parse_native_document(data, 'scan.tiff', {'ocr': True})
        calls = []

        class OCR:
            manifest = {'provider': 'synthetic-no-network'}

            def complete_json(self, *args, **kwargs):
                calls.append(kwargs['images'][0])
                if len(calls) == 2:
                    raise ValueError('synthetic frame failure')
                return {'complete': True, 'unreadable': [], 'blocks': [
                    {'text': f'Recovered frame {len(calls)}', 'bbox': [0, 0, 1, 1]}]}

        first = augment_ocr(data, 'scan.tiff', native, OCR())
        second = augment_ocr(data, 'scan.tiff', first, OCR())
        third = augment_ocr(data, 'scan.tiff', second, OCR())
        self.assertEqual(first['status'], 'PARTIAL')
        self.assertEqual(second['status'], 'READY')
        self.assertEqual(len(calls), 3)
        self.assertEqual([anchor['frame_index'] for anchor in second['anchors']], [0, 1])
        self.assertEqual(second['quality_report']['warnings'], [])
        self.assertEqual(third, second)
        self.assertEqual(native['elements'], [])

    def test_pdf_recovery_preserves_links_order_and_unrelated_missing_capability(self):
        data = pdf_fixture()
        native = parse_native_document(data, 'native.pdf', {'ocr': True})
        report = native['quality_report']
        report['ocr_decisions'] = [{'page_index': index, 'decision': 'REVIEW_REQUIRED'} for index in range(2)]
        report['warnings'] = [{'page_index': index, 'code': 'native_text_quality_requires_review'} for index in range(2)]
        report['missing_capabilities'] = ['native_text_quality_requires_review', 'unrelated_fixture_capability']
        native['status'] = 'PARTIAL'
        count = 0

        class OCR:
            manifest = {'provider': 'synthetic-no-network'}

            def complete_json(self, *args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    raise ValueError('synthetic page failure')
                return {'complete': True, 'unreadable': [], 'blocks': [
                    {'text': 'Bottom', 'bbox': [0, .6, 1, .8]}, {'text': 'Top', 'bbox': [0, .1, 1, .3]}]}

        before = copy.deepcopy(native)
        first = augment_ocr(data, 'native.pdf', native, OCR())
        second = augment_ocr(data, 'native.pdf', first, OCR())
        self.assertEqual(count, 3)
        self.assertEqual(native, before)
        self.assertEqual(second['status'], 'PARTIAL')
        self.assertEqual(second['quality_report']['missing_capabilities'], ['unrelated_fixture_capability'])
        self.assertEqual(second['quality_report']['warnings'], [])
        self.assertEqual([element['raw_text'] for element in second['elements']], ['Top', 'Bottom', 'Top', 'Bottom'])
        self.assertEqual([element['ordinal'] for element in second['elements']], list(range(4)))
        spans = {span['evidence_id']: span for span in second['evidence_spans']}
        anchors = {anchor['anchor_id']: anchor for anchor in second['anchors']}
        order = []
        for element in second['elements']:
            span = spans[element['evidence_ids'][0]]
            self.assertEqual(span['parse_snapshot_id'], second['parse_snapshot_id'])
            self.assertEqual(span['element_ids'], [element['element_id']])
            anchor = anchors[span['anchor_ids'][0]]
            self.assertEqual(anchor['source_version_id'], hashlib.sha256(data).hexdigest())
            order.append(anchor['page_index'])
        self.assertEqual(order, [0, 0, 1, 1])

    def test_page_identity_rejection_retains_only_verified_sibling(self):
        data = pdf_fixture()
        native = parse_native_document(data, 'native.pdf', {'ocr': True})

        def worker(payload, backend):
            if payload.get('mode') == 'identity':
                return dict(IDENTITY)
            return {'identity': dict(IDENTITY), 'results': [parsed_job(payload['jobs'][0]),
                    {**parsed_job(payload['jobs'][1]), 'original_sha256': 'wrong-source'}]}

        with patch.dict(os.environ, {'EVIDENCE_KB_PARSE_ROUTE': 'docling'}), patch(
                'kb_platform.parsing_backends.external_worker', side_effect=worker):
            result = routed_document(data, 'native.pdf', {'ocr': True}, native)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual({anchor['page_index'] for anchor in result['anchors']}, {0})
        self.assertIn('source hash mismatch', result['quality_report']['parser_route']['pages'][1]['diagnostic'])

    def test_failed_and_policy_blocked_inputs_do_not_start_ocr_or_external_parser(self):
        class OCR:
            manifest = {'provider': 'synthetic-no-network'}

            def complete_json(self, *args, **kwargs):
                raise AssertionError('blocked input must not send')

        for data, name, policy in [(b'bad', 'bad.pdf', {'ocr': True}),
                                   (pdf_fixture(), 'native.pdf', {'native_parse': False, 'ocr': False})]:
            native = parse_native_document(data, name, policy)
            with patch('kb_platform.parsing_backends.external_worker', side_effect=AssertionError('must not start')):
                self.assertEqual(routed_document(data, name, policy, native), native)
            self.assertEqual(augment_ocr(data, name, native, OCR()), native)

    def test_page_limit_can_resume_without_spending_limit_on_completed_frames(self):
        stream = io.BytesIO()
        Image.new('RGB', (16, 16), 'white').save(stream, format='TIFF', save_all=True,
            append_images=[Image.new('RGB', (16, 16), 'gray')])
        data = stream.getvalue()
        native = parse_native_document(data, 'limit.tiff', {'ocr': True})
        calls = []

        class OCR:
            manifest = {'provider': 'synthetic-no-network'}

            def complete_json(self, *args, **kwargs):
                calls.append(kwargs['images'][0])
                return {'complete': True, 'unreadable': [], 'blocks': [
                    {'text': 'Recovered', 'bbox': [0, 0, 1, 1]}]}

        with patch('kb_platform.ocr.MAX_OCR_PAGES', 1):
            first = augment_ocr(data, 'limit.tiff', native, OCR())
            second = augment_ocr(data, 'limit.tiff', first, OCR())
        self.assertEqual(first['status'], 'PARTIAL')
        self.assertTrue(any(warning['code'] == 'ocr_page_limit' for warning in first['quality_report']['warnings']))
        self.assertEqual(second['status'], 'READY')
        self.assertEqual(second['quality_report']['warnings'], [])
        self.assertEqual(second['quality_report']['ocr_pages_processed'], [0, 1])
        self.assertEqual(second['quality_report']['model_calls'], 2)
        self.assertEqual(len(calls), 2)


    def test_partial_ensemble_passes_successful_page_identity_to_vision_ocr(self):
        stream = io.BytesIO()
        Image.new('RGB', (32, 32), 'white').save(stream, format='PDF', save_all=True,
            append_images=[Image.new('RGB', (32, 32), 'gray')])
        data = stream.getvalue()
        native = parse_native_document(data, 'handoff.pdf', {'ocr': True})
        self.assertEqual(native['status'], 'PARTIAL')
        self.assertEqual([decision['decision'] for decision in native['quality_report']['ocr_decisions']],
                         ['OCR_PAGE', 'OCR_PAGE'])

        def worker(payload, backend):
            if payload.get('mode') == 'identity':
                return dict(IDENTITY)
            return {'identity': dict(IDENTITY), 'results': [parsed_job(payload['jobs'][0]),
                    {'error': 'synthetic layout failure'}]}

        class OCR:
            manifest = {'provider': 'synthetic-no-network'}

            def __init__(self):
                self.calls = 0

            def complete_json(self, *args, **kwargs):
                self.calls += 1
                return {'complete': True, 'unreadable': [], 'blocks': [
                    {'text': 'Vision fallback text', 'bbox': [0, 0, 1, 1]}]}

        with patch.dict(os.environ, {'EVIDENCE_KB_PARSE_ROUTE': 'docling'}), patch(
                'kb_platform.parsing_backends.external_worker', side_effect=worker):
            ensemble = routed_document(data, 'handoff.pdf', {'ocr': True}, native)
        client = OCR()
        recovered = augment_ocr(data, 'handoff.pdf', ensemble, client)
        self.assertEqual(client.calls, 1)
        self.assertEqual([element['raw_text'] for element in recovered['elements']],
                         ['Synthetic external text', 'Vision fallback text'])
        self.assertEqual(recovered['status'], 'PARTIAL')
        self.assertIn('selected_external_page_parser_failed', recovered['quality_report']['missing_capabilities'])
        self.assertTrue(any(warning['code'] == 'selected_external_parser_failed'
                            for warning in recovered['quality_report']['warnings']))
        self.assertEqual(recovered['quality_report']['parser_route']['pages'][1]['status'], 'FAILED')



class PageRecoveryLintContracts(unittest.TestCase):
    def test_regression_mutations_fail_mandatory_lint_after_hash_refresh(self):
        import json
        from pathlib import Path
        import shutil
        from .formal_state_machines import registry_violations, source_digest
        from .page_recovery_rules import page_recovery_violations

        root = Path(__file__).resolve().parents[2]
        self.assertEqual(page_recovery_violations(root), [])
        registry = json.loads((root / 'formal/registry-v1.json').read_text('utf-8'))
        reviewed = next(machine for machine in registry['machines'] if machine['id'] == 'page_recovery')
        mutations = [
            ('page_parsing.py', '_accept_page_result(row, cached, identity, True)', 'row.update(status="COMPLETE", cache_hit=True)'),
            ('page_parsing.py', 'conversion.split(".")[-1] != "SUCCESS"', 'False'),
            ('page_parsing.py', 'parsed.get("original_sha256") != row["page_binary_sha256"]', 'False'),
            ('page_parsing.py', 'row["attempts"].append({"backend": fallback, "status": "FAILED", "diagnostic": diagnostic})', 'break'),
            ('page_parsing.py', '    for index in completed:\n        quality["ocr_decisions"].append({"page_index": index, "decision": "LOCAL_PAGE_PARSE_COMPLETED"})',
             '    if not failures:\n        for index in completed:\n            quality["ocr_decisions"].append({"page_index": index, "decision": "LOCAL_PAGE_PARSE_COMPLETED"})'),
            ('ocr.py', 'sorted(needed - completed)', 'sorted(needed)'),
            ('ocr.py', 'if index in completed:', 'if False:'),
            ('ocr.py', 'if not attempts and not failures:', 'if False:'),
            ('ocr.py', 'item.get("page_index") in completed and (item.get("stage") == "ocr"',
             'True and (item.get("stage") == "ocr"'),
        ]
        for filename, before, after in mutations:
            with self.subTest(mutation=before), TemporaryDirectory() as directory:
                target_root = Path(directory)
                machine = copy.deepcopy(reviewed)
                paths = set(machine['owners'])
                paths.update(model[key] for model in machine['models'] + machine['negative_controls']
                             for key in ('module', 'config'))
                for path in paths:
                    destination = target_root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / path, destination)
                target = target_root / 'kb_platform' / filename
                source = target.read_text('utf-8')
                self.assertIn(before, source)
                target.write_text(source.replace(before, after), 'utf-8')
                for binding in machine['source_bindings']:
                    binding['sha256'] = source_digest(target_root, binding)
                mutated = {'schema_version': 1, 'machines': [machine],
                           'discovery': {'roots': ['kb_platform'], 'exclusions': []}}
                errors = registry_violations(target_root, mutated)
                self.assertFalse(any('source review drift' in error for error in errors), errors)
                self.assertTrue(any(error.startswith('page_recovery:') for error in errors), errors)
