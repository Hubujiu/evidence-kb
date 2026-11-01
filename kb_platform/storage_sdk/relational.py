from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Protocol

from sqlalchemy import JSON, DateTime, MetaData, String
from sqlalchemy.engine.default import StrCompileDialect
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.functions import GenericFunction
from sqlalchemy.types import TypeDecorator

from ..database_contracts import QueryLimits, StorageIdentity
from . import Contract


IDENTIFIER = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')


class TypedJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self):
        super().__init__(none_as_null=True)

    def process_bind_param(self, value, dialect):
        if value is not None:
            json.dumps(value, ensure_ascii=True, allow_nan=False)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            json.dumps(value, ensure_ascii=True, allow_nan=False)
        return value


class ExactString(TypeDecorator):
    impl = String
    cache_ok = True

    def __init__(self, length):
        if type(length) is not int or length < 1:
            raise ValueError('INVALID_STORAGE_STRING_LENGTH')
        self.length = length
        super().__init__(length)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > self.length:
            raise ValueError('STORAGE_STRING_LIMIT')
        return value


class UtcDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError('STORAGE_UTC_DATETIME_REQUIRED')
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Clock(GenericFunction):
    name = 'storage_clock'
    type = UtcDateTime()
    inherit_cache = True


class After(GenericFunction):
    name = 'storage_after'
    type = UtcDateTime()
    inherit_cache = True


@dataclass(frozen=True)
class StructuredSchema:
    owner: str
    metadata: MetaData
    version: int = 1
    appendonly: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.owner, str) or not self.owner or not isinstance(self.metadata, MetaData)
                or self.version != 1 or not self.metadata.tables):
            raise ValueError('INVALID_STRUCTURED_SCHEMA')
        object.__setattr__(self, 'appendonly', tuple(self.appendonly))
        for table in self.metadata.tables.values():
            if (not IDENTIFIER.fullmatch(table.name) or table.schema is not None
                    or table.name.startswith(('_database_', '_storage_')) or not tuple(table.primary_key)):
                raise ValueError('INVALID_STRUCTURED_TABLE')
            if any(not IDENTIFIER.fullmatch(column.name) for column in table.columns):
                raise ValueError('INVALID_STRUCTURED_COLUMN')
            scope = tuple(table.info.get('scope', ()))
            if any(name not in table.c or not IDENTIFIER.fullmatch(name) for name in scope):
                raise ValueError('INVALID_STRUCTURED_SCOPE')
        if any(name not in self.metadata.tables for name in self.appendonly):
            raise ValueError('INVALID_STRUCTURED_APPENDONLY')

    @property
    def digest(self):
        dialect = StrCompileDialect()
        definitions = []
        for table in sorted(self.metadata.tables.values(), key=lambda value: value.name):
            definitions.append([table.name, str(CreateTable(table).compile(dialect=dialect)),
                                sorted(str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes),
                                [[column.name, type(column.type).__module__, type(column.type).__qualname__]
                                 for column in table.columns],
                                list(table.info.get('scope', ()))])
        payload = [self.owner, self.version, definitions, sorted(self.appendonly)]
        return sha256(json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


class StructuredRelationalStore(Protocol):
    identity: StorageIdentity

    def prepare(self, package: StructuredSchema): ...
    def seal(self): ...
    def transaction(self, *, write: bool = True, scope=None, limits: QueryLimits | None = None,
                    operation_id=None) -> AbstractContextManager: ...


STRUCTURED_RELATIONAL_V1 = Contract('storage.relational.structured', 1, interface=StructuredRelationalStore)


class GuardedRelationalStore(StructuredRelationalStore, Protocol):
    def guard(self, name: str, *, nowait: bool = True) -> AbstractContextManager: ...


GUARDED_RELATIONAL_V1 = Contract('storage.relational.guarded', 1,
                                (STRUCTURED_RELATIONAL_V1,), GuardedRelationalStore)
