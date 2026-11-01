import base64
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import math

from sqlalchemy.exc import SQLAlchemyError

from .database_contracts import CommitUnknown, DatabaseError, QueryLimits, SqlStatement


_PROCESS_LIMITS = QueryLimits(max_rows=1000, max_bytes=256 * 1024, max_statements=100, max_seconds=30)


def encode_value(value):
    if value is None or type(value) in {str, bool}:
        return value
    if type(value) is int:
        return {'type': 'integer', 'value': str(value)}
    if type(value) is float:
        if not math.isfinite(value):
            raise DatabaseError('INVALID_DATABASE_NUMBER')
        return {'type': 'float', 'value': repr(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DatabaseError('INVALID_DATABASE_NUMBER')
        return {'type': 'decimal', 'value': str(value)}
    if isinstance(value, bytes):
        return {'type': 'bytes', 'value': base64.b64encode(value).decode('ascii')}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DatabaseError('DATABASE_DATETIME_REQUIRES_TIMEZONE')
        return {'type': 'datetime', 'value': value.astimezone(timezone.utc).isoformat()}
    if isinstance(value, date):
        return {'type': 'date', 'value': value.isoformat()}
    if isinstance(value, (dict, list)):
        json.dumps(value, allow_nan=False)
        return {'type': 'json', 'value': value}
    raise DatabaseError('UNSUPPORTED_DATABASE_VALUE')


def decode_value(value):
    if value is None or type(value) in {str, bool}:
        return value
    if not isinstance(value, dict) or set(value) != {'type', 'value'}:
        raise DatabaseError('INVALID_DATABASE_TYPED_VALUE')
    kind, raw = value['type'], value['value']
    try:
        if kind == 'integer' and isinstance(raw, str):
            return int(raw)
        if kind == 'float' and isinstance(raw, str):
            result = float(raw)
            if math.isfinite(result):
                return result
        elif kind == 'decimal' and isinstance(raw, str):
            result = Decimal(raw)
            if result.is_finite():
                return result
        elif kind == 'bytes' and isinstance(raw, str):
            return base64.b64decode(raw, validate=True)
        elif kind == 'datetime' and isinstance(raw, str):
            result = datetime.fromisoformat(raw)
            if result.tzinfo is not None and result.utcoffset() is not None:
                return result.astimezone(timezone.utc)
        elif kind == 'date' and isinstance(raw, str):
            return date.fromisoformat(raw)
        elif kind == 'json' and isinstance(raw, (dict, list)):
            return json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, ArithmeticError):
        pass
    raise DatabaseError('INVALID_DATABASE_TYPED_VALUE')


def _statement(value):
    if not isinstance(value, dict) or set(value) - {'sql', 'params', 'expected_rows'}:
        raise DatabaseError('INVALID_DATABASE_STATEMENT')
    sql, params = value.get('sql'), value.get('params', {})
    if (not isinstance(sql, str) or not sql.strip() or len(sql.encode()) > 65536 or
            not isinstance(params, dict) or any(not isinstance(key, str) for key in params)):
        raise DatabaseError('INVALID_DATABASE_STATEMENT')
    return SqlStatement(sql, {key: decode_value(item) for key, item in params.items()}, value.get('expected_rows'))


def _request(request, fields):
    if not isinstance(request, dict) or set(request) - set(fields) - {'limits'}:
        raise DatabaseError('INVALID_DATABASE_REQUEST')
    try:
        if len(json.dumps(request, allow_nan=False).encode()) > 512 * 1024:
            raise DatabaseError('DATABASE_REQUEST_LIMIT')
        raw = request.get('limits', {})
        if not isinstance(raw, dict) or set(raw) - {'max_rows', 'max_bytes', 'max_statements', 'max_seconds'}:
            raise DatabaseError('INVALID_DATABASE_LIMITS')
        selected = QueryLimits(**{name: raw.get(name, getattr(_PROCESS_LIMITS, name)) for name in
                                  ('max_rows', 'max_bytes', 'max_statements', 'max_seconds')})
        if any(getattr(selected, name) > getattr(_PROCESS_LIMITS, name) for name in
               ('max_rows', 'max_bytes', 'max_statements', 'max_seconds')):
            raise DatabaseError('DATABASE_QUERY_LIMIT')
    except (ValueError, TypeError):
        raise DatabaseError('INVALID_DATABASE_REQUEST') from None
    return selected


def _result(result):
    return {'rows': [{key: encode_value(value) for key, value in row.items()} for row in result.rows],
            'rowcount': result.rowcount}


def query_json(database, request):
    limits = _request(request, {'statement'})
    statement = _statement(request.get('statement'))
    try:
        with database.transaction(write=False, limits=limits) as tx:
            result = _result(tx.execute(statement))
            _check_result_size(result, limits)
            return result
    except SQLAlchemyError:
        raise DatabaseError('DATABASE_QUERY_FAILED') from None


def batch_json(database, request):
    limits = _request(request, {'statements', 'operation_id'})
    operation_id = request.get('operation_id')
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
        raise DatabaseError('DATABASE_OPERATION_ID_REQUIRED')
    values = request.get('statements')
    if not isinstance(values, list) or not values or len(values) > limits.max_statements:
        raise DatabaseError('DATABASE_BATCH_LIMIT')
    statements = tuple(_statement(value) for value in values)
    try:
        with database.transaction(write=True, limits=limits, operation_id=operation_id) as tx:
            result = {'operation_id': operation_id, 'status': 'COMMITTED',
                      'results': [_result(tx.execute(statement)) for statement in statements]}
            _check_result_size(result, limits)
        return result
    except CommitUnknown:
        raise
    except SQLAlchemyError:
        raise DatabaseError('DATABASE_BATCH_ROLLED_BACK') from None


def _check_result_size(result, limits):
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > limits.max_bytes:
        raise DatabaseError('DATABASE_RESULT_LIMIT')
