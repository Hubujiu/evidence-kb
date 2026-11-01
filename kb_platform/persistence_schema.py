from .database_contracts import Migration, SchemaPackage


def cache_schema(owner, engine='sqlite'):
    key = '`key`' if engine == 'mysql' else 'key'
    return SchemaPackage(owner, (Migration(1, (
        'CREATE TABLE entries (' + key + ' VARCHAR(64) PRIMARY KEY, hash VARCHAR(64) NOT NULL, object_ref TEXT)',
    )),))


def budget_schema(owner, engine='sqlite'):
    identity = 'VARBINARY(800)' if engine == 'mysql' else 'VARCHAR(200)'
    usage = '`usage`' if engine == 'mysql' else 'usage'
    return SchemaPackage(owner, (Migration(1, (
        'CREATE TABLE config (id INTEGER PRIMARY KEY, caps TEXT NOT NULL)',
        'CREATE TABLE calls (id ' + identity + ' PRIMARY KEY, status VARCHAR(16) NOT NULL, '
        'reserved TEXT NOT NULL, ' + usage + ' TEXT, category TEXT NOT NULL, diagnostic TEXT)',
        'CREATE TABLE producer_guard (id INTEGER PRIMARY KEY, successor TEXT)',
        'INSERT INTO producer_guard(id,successor) VALUES(1,NULL)',
        'CREATE TABLE ledger_imports (source_sha256 VARCHAR(64) PRIMARY KEY, '
        'snapshot_sha256 VARCHAR(64) NOT NULL, source_path TEXT NOT NULL, record_count INTEGER NOT NULL)',
    )),))
