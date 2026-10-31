import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from .session_identity import PlatformError


def parse(data: bytes, filename: str, policy: dict) -> dict:
    if len(data) <= 64 * 1024 and Path(filename).suffix.lower() in {'.md', '.txt'}:
        try:
            from .documents import parse_document

            return parse_document(data, filename, policy)
        except Exception as exc:
            raise PlatformError('PARSE_FAILED', 'Document parsing failed', 422) from exc
    code = ('import base64,json,sys; from kb_platform.documents import parse_document; '
            'x=json.load(sys.stdin); '
            'print(json.dumps(parse_document(base64.b64decode(x["data"]),x["filename"],x["policy"])))')
    payload = json.dumps({'data': base64.b64encode(data).decode(), 'filename': filename, 'policy': policy})
    try:
        timeout = max(1, min(3600, float(os.environ.get('EVIDENCE_KB_PARSE_TIMEOUT_SECONDS', '45'))))
        result = subprocess.run([sys.executable, '-c', code], input=payload, capture_output=True,
                                encoding='utf-8', timeout=timeout, cwd=Path(__file__).resolve().parent.parent)
    except subprocess.TimeoutExpired as exc:
        raise PlatformError('PARSE_TIMEOUT', 'Document parsing exceeded its configured timeout', 422) from exc
    if result.returncode:
        raise PlatformError('PARSE_FAILED', 'Document parsing failed', 422)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise PlatformError('PARSE_FAILED', 'Document parser returned invalid output', 422) from exc
