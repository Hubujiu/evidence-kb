from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import inspect, text

from .database_contracts import DatabaseError, StorageIdentity


@contextmanager
def migration_connection(space):
    connection = space.migration_engine.connect()
    mysql_lock = False
    failure = None
    try:
        if space.info.engine == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        elif space.info.engine == 'postgresql':
            connection.begin()
            connection.execute(text('SELECT pg_advisory_xact_lock(:key)'),
                               {'key': int(space.key.digest[:15], 16)})
            connection.execute(text("SELECT set_config('search_path', :path, true)"),
                               {'path': space.physical_name + ', pg_catalog'})
        else:
            locked = connection.execute(text('SELECT GET_LOCK(:name,30)'),
                                        {'name': 'migration_' + space.key.digest[:48]}).scalar()
            if locked != 1:
                raise DatabaseError('DATABASE_MIGRATION_BUSY')
            mysql_lock = True
        yield connection
        connection.commit()
    except BaseException as error:
        failure = error
        try:
            connection.rollback()
        except BaseException as rollback_error:
            failure.add_note('Migration rollback failed: ' + type(rollback_error).__name__)
        raise
    finally:
        try:
            if mysql_lock:
                connection.execute(text('SELECT RELEASE_LOCK(:name)'),
                                   {'name': 'migration_' + space.key.digest[:48]})
                connection.commit()
        except BaseException as release_error:
            if failure is None:
                failure = release_error
            else:
                failure.add_note('Migration lock release failed: ' + type(release_error).__name__)
            try:
                connection.invalidate()
            except BaseException as invalidate_error:
                failure.add_note('Migration invalidation failed: ' + type(invalidate_error).__name__)
            if failure is release_error:
                raise
        finally:
            try:
                connection.close()
            except BaseException as close_error:
                if failure is None:
                    raise
                failure.add_note('Migration close failed: ' + type(close_error).__name__)


def initialize_space(space, *, expected_dataset_id=None):
    with migration_connection(space) as conn:
        inspector = inspect(conn)
        if not inspector.has_table('_database_identity', schema=None if space.info.engine == 'sqlite' else space.physical_name):
            conn.exec_driver_sql('CREATE TABLE IF NOT EXISTS _database_identity '
                             '(identity_key INTEGER PRIMARY KEY, space_key VARCHAR(1600) NOT NULL, '
                             'dataset_id VARCHAR(64) NOT NULL)')
        if not inspector.has_table('_database_migrations', schema=None if space.info.engine == 'sqlite' else space.physical_name):
            conn.exec_driver_sql('CREATE TABLE IF NOT EXISTS _database_migrations '
                             '(schema_owner VARCHAR(200) NOT NULL, version INTEGER NOT NULL, '
                             'content_hash VARCHAR(64) NOT NULL, dirty INTEGER NOT NULL, '
                             'step INTEGER NOT NULL, PRIMARY KEY(schema_owner,version))')
        row = conn.execute(text('SELECT space_key,dataset_id FROM _database_identity WHERE identity_key=1')).first()
        if row is None:
            if expected_dataset_id is not None:
                raise DatabaseError('DATABASE_EXPECTED_DATASET_MISSING')
            dataset_id = uuid4().hex
            conn.execute(text('INSERT INTO _database_identity(identity_key,space_key,dataset_id) '
                              'VALUES(1,:space,:dataset)'), {'space': space.key.key, 'dataset': dataset_id})
        else:
            if row.space_key != space.key.key:
                raise DatabaseError('DATABASE_SPACE_IDENTITY_MISMATCH')
            dataset_id = row.dataset_id
            if expected_dataset_id is not None and dataset_id != expected_dataset_id:
                raise DatabaseError('DATABASE_DATASET_IDENTITY_MISMATCH')
    space.identity = StorageIdentity(space.key, dataset_id)
    return space.identity


def prepare(space, package):
    if package.owner != space.key.owner_id:
        raise DatabaseError('DATABASE_SCHEMA_OWNER_MISMATCH')
    applied = []
    with migration_connection(space) as conn:
        records = {row.version: row for row in conn.execute(text(
            'SELECT version,content_hash,dirty,step FROM _database_migrations WHERE schema_owner=:owner'),
            {'owner': package.owner})}
        if any(version > len(package.migrations) for version in records):
            raise DatabaseError('DATABASE_SCHEMA_DOWNGRADE')
        for migration in package.migrations:
            record = records.get(migration.version)
            if record is not None:
                if record.content_hash != migration.digest:
                    raise DatabaseError('DATABASE_MIGRATION_HASH_MISMATCH')
                if record.dirty:
                    raise DatabaseError('DATABASE_MIGRATION_DIRTY')
                continue
            values = {'owner': package.owner, 'version': migration.version, 'hash': migration.digest}
            conn.execute(text('INSERT INTO _database_migrations(schema_owner,version,content_hash,dirty,step) '
                              'VALUES(:owner,:version,:hash,1,0)'), values)
            mysql = space.info.engine == 'mysql'
            if mysql:
                conn.commit()
            for step, sql in enumerate(migration.statements, 1):
                quote = conn.dialect.identifier_preparer.quote
                expanded = sql.replace('__DATABASE_SCHEMA__', quote(space.physical_name))
                if space.runtime_engine.url.username:
                    expanded = expanded.replace('__DATABASE_RUNTIME_ROLE__', quote(space.runtime_engine.url.username))
                raw = conn.connection.driver_connection
                if space.info.engine == 'sqlite':
                    conn.exec_driver_sql(expanded)
                else:
                    cursor = raw.cursor()
                    try:
                        if space.info.engine == 'postgresql':
                            cursor.execute(expanded, prepare=False)
                        else:
                            cursor.execute(expanded)
                    finally:
                        cursor.close()
                conn.execute(text('UPDATE _database_migrations SET step=:step '
                                  'WHERE schema_owner=:owner AND version=:version'), {**values, 'step': step})
                if mysql:
                    conn.commit()
            conn.execute(text('UPDATE _database_migrations SET dirty=0 '
                              'WHERE schema_owner=:owner AND version=:version'), values)
            if mysql:
                conn.commit()
            applied.append(migration.version)
    space.grant_runtime(package)
    return tuple(applied)
