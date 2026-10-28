from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4


class OriginalStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, tenant_id: str, data: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        tenant_key = hashlib.sha256(tenant_id.encode()).hexdigest()
        folder = self.root / tenant_key / digest[:2]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / digest
        temporary = folder / f'.{uuid4().hex}.tmp'
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError('Original integrity failure')
        finally:
            temporary.unlink(missing_ok=True)
        return str(target), digest

    def checked_path(self, path: str, tenant_id: str, digest: str) -> Path:
        expected = self.root / hashlib.sha256(tenant_id.encode()).hexdigest() / digest[:2] / digest
        actual = Path(path).resolve()
        if actual != expected or hashlib.sha256(actual.read_bytes()).hexdigest() != digest:
            raise ValueError('Original integrity failure')
        return actual
