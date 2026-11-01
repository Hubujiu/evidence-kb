import hashlib
import json
from pathlib import Path

from .search import OpenSearch, SearchUnavailable, backend_dependencies, content_order
from .implementation_dependencies import implementation_dependencies
from .storage_sdk import StorageError


def canonical_units(units):
    return json.dumps(sorted(units, key=content_order), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def unit_sha256(unit):
    return hashlib.sha256(canonical_units([unit]).encode()).hexdigest()


class DocumentEvidenceSource:
    supports_resume_build = True
    supports_manifest_storage = True

    def __init__(self, documents):
        self.documents = documents
        self.identity = documents.identity

    def storage_identity(self):
        return {'dataset': str(self.identity)}

    def dependency_manifest(self):
        return {'version': 1, 'implementation': implementation_dependencies(__file__,
            Path(__file__).with_name('search.py')), 'storage': self.storage_identity(),
            'documents': backend_dependencies(self.documents)}

    @staticmethod
    def _key(index, suffix):
        OpenSearch._index(index)
        return 'evidence:' + index + ':' + suffix

    def _put_immutable(self, key, value):
        existing = self.documents.get(key)
        if existing is not None:
            if existing.value != value:
                raise SearchUnavailable('Existing evidence differs from frozen input')
            return
        self.documents.put(key, value, expected_revision=0)

    def stage(self, index, units, vector_dimension=None):
        units = json.loads(canonical_units(units))
        if not units or len({unit['unit_id'] for unit in units}) != len(units):
            raise SearchUnavailable('Nonempty unique units required')
        scope = (units[0]['tenant_id'], units[0]['kb_id'])
        if any((unit['tenant_id'], unit['kb_id']) != scope for unit in units):
            raise SearchUnavailable('Mixed generation scopes')
        manifest = {'dimension': vector_dimension, 'count': len(units),
                    'tenant_id': scope[0], 'kb_id': scope[1],
                    'units_sha256': hashlib.sha256(canonical_units(units).encode()).hexdigest(),
                    'records': [[unit['unit_id'], unit_sha256(unit)] for unit in units]}
        if self.documents.get(self._key(index, 'seal')) is not None:
            raise SearchUnavailable('Sealed evidence cannot be replaced')
        try:
            for unit in units:
                self._put_immutable(self._key(index, unit_sha256(unit)), unit)
            self._put_immutable(self._key(index, 'manifest'), manifest)
        except StorageError as exc:
            raise SearchUnavailable('Evidence staging failed: ' + exc.code) from exc
        self.verify(index)

    def _read(self, index):
        try:
            return self._read_snapshot(index)
        except (KeyError, TypeError, ValueError, StorageError) as exc:
            raise SearchUnavailable('Evidence snapshot integrity mismatch') from exc

    def _read_snapshot(self, index):
        document = self.documents.get(self._key(index, 'manifest'))
        if document is None:
            raise SearchUnavailable('Search generation not found')
        manifest = document.value
        rows = manifest['records']
        units = []
        for start in range(0, len(rows), 8):
            batch = rows[start:start + 8]
            documents = self.documents.get_many(tuple(self._key(index, sha) for _, sha in batch))
            if len(documents) != len(batch):
                raise SearchUnavailable('Incomplete evidence snapshot')
            for (id, sha), value in zip(batch, documents, strict=True):
                if value is None or value.value.get('unit_id') != id or unit_sha256(value.value) != sha:
                    raise SearchUnavailable('Evidence snapshot integrity mismatch')
                units.append(value.value)
        if (len(units) != manifest['count'] or len({unit['unit_id'] for unit in units}) != len(units)
                or any((unit['tenant_id'], unit['kb_id']) != (manifest['tenant_id'], manifest['kb_id']) for unit in units)
                or hashlib.sha256(canonical_units(units).encode()).hexdigest() != manifest['units_sha256']):
            raise SearchUnavailable('Evidence snapshot integrity mismatch')
        return manifest, units

    def read_units(self, index, ids=None):
        _, units = self._read(index)
        if ids is None:
            return units
        by_id = {unit['unit_id']: unit for unit in units}
        return [by_id[id] for id in ids if id in by_id]

    def verify(self, index):
        manifest, _ = self._read(index)
        return {key: value for key, value in manifest.items() if key != 'records'} | {'storage': self.storage_identity()}

    def seal(self, index, checksum):
        manifest = self.verify(index)
        self._put_immutable(self._key(index, 'seal'), {'checksum': checksum, 'snapshot': manifest})

    def is_sealed(self, index, checksum):
        try:
            seal = self.documents.get(self._key(index, 'seal'))
            return seal is not None and seal.value == {'checksum': checksum, 'snapshot': self.verify(index)}
        except (SearchUnavailable, StorageError):
            return False

    @staticmethod
    def _pointer(tenant_id, kb_id):
        return 'evidence:active:' + hashlib.sha256(json.dumps([tenant_id, kb_id]).encode()).hexdigest()

    def publish_manifest(self, manifest, checksum, *, index=None):
        index = manifest['index_name'] if index is None else index
        snapshot = self.verify(index)
        if (not self.is_sealed(index, checksum)
                or (manifest['tenant_id'], manifest['kb_id']) != (snapshot['tenant_id'], snapshot['kb_id'])):
            raise SearchUnavailable('Evidence must be sealed in the requested scope before publication')
        key = self._pointer(manifest['tenant_id'], manifest['kb_id'])
        existing = self.documents.get(key)
        self.documents.put(key, {'manifest': manifest, 'checksum': checksum},
                           expected_revision=0 if existing is None else existing.revision)

    def current_manifest(self, tenant_id, kb_id):
        current = self.documents.get(self._pointer(tenant_id, kb_id))
        if current is None:
            raise FileNotFoundError('No published semantic manifest')
        manifest, checksum = current.value['manifest'], current.value['checksum']
        if ((manifest['tenant_id'], manifest['kb_id']) != (tenant_id, kb_id)
                or not self.is_sealed(manifest['index_name'], checksum)):
            raise SearchUnavailable('Published evidence is incomplete or corrupt')
        return manifest, checksum
