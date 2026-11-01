from dataclasses import dataclass, field
from hashlib import sha256
import json
from types import MappingProxyType


class DatabaseError(RuntimeError):
    def __init__(self, message):
        self.code = message.split(':', 1)[0]
        super().__init__(message)


class CommitUnknown(DatabaseError):
    def __init__(self, operation_id):
        self.operation_id = operation_id
        super().__init__('COMMIT_UNKNOWN:' + operation_id)


@dataclass(frozen=True)
class SpaceKey:
    application_id: str
    owner_id: str
    space_id: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200
               for value in (self.application_id, self.owner_id, self.space_id)):
            raise ValueError('INVALID_DATABASE_SPACE')

    @property
    def key(self):
        return json.dumps([self.application_id, self.owner_id, self.space_id], ensure_ascii=True,
                          separators=(',', ':'))

    @property
    def digest(self):
        return sha256(self.key.encode()).hexdigest()


@dataclass(frozen=True)
class StorageIdentity:
    space: SpaceKey
    dataset_id: str


@dataclass(frozen=True)
class DatabaseInfo:
    engine: str
    capabilities: frozenset[str] = frozenset({'transactions', 'named_parameters', 'bounded_results'})


@dataclass(frozen=True)
class QueryLimits:
    max_rows: int = 10000
    max_bytes: int = 8 * 1024 * 1024
    max_statements: int = 100
    max_seconds: float = 30

    def __post_init__(self):
        import math

        if (any(type(value) is not int or value < 1 for value in
                (self.max_rows, self.max_bytes, self.max_statements)) or
                type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds) or
                self.max_seconds <= 0 or self.max_seconds > 300):
            raise ValueError('INVALID_DATABASE_LIMITS')


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]

    def __post_init__(self):
        if type(self.version) is not int or self.version < 1 or not self.statements:
            raise ValueError('INVALID_DATABASE_MIGRATION')
        object.__setattr__(self, 'statements', tuple(self.statements))
        if any(not isinstance(sql, str) or not sql.strip() for sql in self.statements):
            raise ValueError('INVALID_DATABASE_MIGRATION')

    @property
    def digest(self):
        return sha256(json.dumps([self.version, self.statements], separators=(',', ':'),
                                 ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class SchemaPackage:
    owner: str
    migrations: tuple[Migration, ...]
    runtime_readonly: tuple[str, ...] = ()
    runtime_functions: tuple[str, ...] = ()
    runtime_appendonly: tuple[str, ...] = ()
    runtime_readonly_prefixes: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'migrations', tuple(self.migrations))
        versions = [migration.version for migration in self.migrations]
        if not self.owner or not versions or versions != list(range(1, len(versions) + 1)):
            raise ValueError('INVALID_DATABASE_SCHEMA_PACKAGE')
        object.__setattr__(self, 'runtime_readonly', tuple(self.runtime_readonly))
        object.__setattr__(self, 'runtime_functions', tuple(self.runtime_functions))
        object.__setattr__(self, 'runtime_appendonly', tuple(self.runtime_appendonly))
        object.__setattr__(self, 'runtime_readonly_prefixes', tuple(self.runtime_readonly_prefixes))
        if any(not isinstance(prefix, str) or not prefix for prefix in self.runtime_readonly_prefixes):
            raise ValueError('INVALID_DATABASE_RUNTIME_POLICY')


@dataclass(frozen=True)
class SqlStatement:
    sql: str
    params: dict = field(default_factory=dict)
    expected_rows: int | None = None

    def __post_init__(self):
        if (not isinstance(self.sql, str) or not self.sql.strip() or not isinstance(self.params, dict) or
                any(not isinstance(key, str) for key in self.params) or self.expected_rows is not None and
                (type(self.expected_rows) is not int or self.expected_rows < 0)):
            raise ValueError('INVALID_DATABASE_STATEMENT')
        object.__setattr__(self, 'params', MappingProxyType(dict(self.params)))


class QueryRow(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class QueryResult:
    def __init__(self, rows=(), rowcount=0):
        self.rows = tuple(QueryRow(row) for row in rows)
        self.rowcount = rowcount
        self._offset = 0

    def fetchone(self):
        if self._offset >= len(self.rows):
            return None
        row = self.rows[self._offset]
        self._offset += 1
        return row

    def fetchall(self):
        rows = list(self.rows[self._offset:])
        self._offset = len(self.rows)
        return rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)
