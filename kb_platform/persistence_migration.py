from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .rag_testing.budget import BudgetLedger, producer_guard, validate_caps, validate_resources
from .rag_testing.contracts import digest


def inspect_ledger_import(path):
    path = Path(path).resolve(strict=True)
    before = sha256(path.read_bytes()).hexdigest()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        caps = json.loads(db.execute('SELECT caps FROM config WHERE id=1').fetchone()[0])
        validate_caps(caps)
        rows = [dict(zip(('id', 'status', 'reserved', 'usage', 'category', 'diagnostic'), row, strict=True))
                for row in db.execute('SELECT id,status,reserved,usage,category,diagnostic FROM calls ORDER BY id')]
    for row in rows:
        if (not isinstance(row['id'], str) or not row['id'] or len(row['id']) > 200 or
                row['status'] not in {'STARTED', 'COMPLETE', 'ERROR', 'UNKNOWN'} or
                not isinstance(row['category'], str)):
            raise ValueError('INVALID_LEDGER_IMPORT_RECORD')
        validate_resources(caps, json.loads(row['reserved']), complete=True)
        if row['usage'] is not None:
            validate_resources(caps, json.loads(row['usage']), complete=False)
    if sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError('LEDGER_CHANGED_DURING_IMPORT_SNAPSHOT')
    return {'path': str(path), 'file_sha256': before, 'caps': caps, 'rows': rows,
            'snapshot_sha256': digest({'caps': caps, 'rows': rows})}


def import_ledger(path, database, *, expected_file_sha256, expected_snapshot_sha256):
    path = Path(path).resolve(strict=True)
    if database.info.engine == 'sqlite' and Path(database.physical_name).resolve() == path:
        raise ValueError('LEDGER_IMPORT_REQUIRES_DISTINCT_DESTINATION')
    with producer_guard(path, allow_sealed=True) as producer:
        frozen = inspect_ledger_import(path)
        if (frozen['file_sha256'] != expected_file_sha256 or
                frozen['snapshot_sha256'] != expected_snapshot_sha256):
            raise ValueError('LEDGER_IMPORT_SOURCE_CHANGED')
        destination = 'dataset:' + database.identity.dataset_id
        predecessor = producer.execute('SELECT successor FROM producer WHERE id=1').fetchone()
        if predecessor is not None and predecessor[0] != destination:
            raise ValueError('LEDGER_IMPORT_DESTINATION_CHANGED')
        if predecessor is None:
            producer.execute('INSERT INTO producer(id,successor) VALUES(1,?)', (destination,))
            producer.commit()
            producer.execute('BEGIN IMMEDIATE')
        ledger = BudgetLedger(caps=frozen['caps'], database=database)
        with ledger.admission(), ledger._db() as db:
            imported = db.execute('SELECT snapshot_sha256 FROM ledger_imports WHERE source_sha256=:sha',
                                 {'sha': frozen['file_sha256']}).fetchone()
            if imported is not None:
                if imported['snapshot_sha256'] != frozen['snapshot_sha256']:
                    raise ValueError('LEDGER_IMPORT_MAPPING_CHANGED')
                return ledger
            if db.execute('SELECT 1 FROM calls LIMIT 1').fetchone():
                raise ValueError('LEDGER_IMPORT_DESTINATION_NOT_EMPTY')
            for row in frozen['rows']:
                db.execute('INSERT INTO calls(id,status,reserved,' + ledger._usage_column + ',category,diagnostic) '
                           'VALUES(:id,:status,:reserved,:usage,:category,:diagnostic)', row)
            db.execute('INSERT INTO ledger_imports(source_sha256,snapshot_sha256,source_path,record_count) '
                       'VALUES(:sha,:snapshot,:path,:count)', {'sha': frozen['file_sha256'],
                       'snapshot': frozen['snapshot_sha256'], 'path': str(path), 'count': len(frozen['rows'])})
        current = inspect_ledger_import(path)
        if current != frozen:
            raise ValueError('LEDGER_IMPORT_SOURCE_CHANGED')
    return ledger


def validate_ledger_binding(ledger, source):
    frozen = inspect_ledger_import(source)
    with ledger.database.transaction(write=False) as db:
        row = db.execute('SELECT snapshot_sha256 FROM ledger_imports '
                         'WHERE source_sha256=:sha', {'sha': frozen['file_sha256']}).fetchone()
    if row is None or row['snapshot_sha256'] != frozen['snapshot_sha256']:
        raise ValueError('MODEL_BUDGET_PATH_REQUIRES_EXPLICIT_BINDING_MIGRATION')
    with producer_guard(source, allow_sealed=True) as producer:
        row = producer.execute('SELECT successor FROM producer WHERE id=1').fetchone()
        if row is None or row[0] != 'dataset:' + ledger.database.identity.dataset_id:
            raise ValueError('MODEL_BUDGET_SOURCE_NOT_SEALED_FOR_BINDING')
