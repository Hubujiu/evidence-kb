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
