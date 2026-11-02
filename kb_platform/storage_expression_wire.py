import json

from sqlalchemy import BigInteger, Boolean, Date, Float, Integer, LargeBinary, Numeric, String, Text
from sqlalchemy import and_, case, delete, func, literal, not_, or_, select, update
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import (
    BinaryExpression, BindParameter, BooleanClauseList, Case, ColumnClause, False_, Grouping, Label,
    Null, True_, UnaryExpression,
)
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.schema import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint
from sqlalchemy.sql.selectable import Alias, Join, Select
from sqlalchemy.sql.dml import Delete, Update
from sqlalchemy.sql.sqltypes import NullType

from .database_contracts import DatabaseError
from .database_process import decode_value, encode_value
from .storage_sdk.relational import After, Clock, ExactString, TypedJSON, UtcDateTime


BINARY = {'eq': operators.eq, 'ne': operators.ne, 'lt': operators.lt, 'le': operators.le,
          'gt': operators.gt, 'ge': operators.ge, 'add': operators.add, 'sub': operators.sub,
          'mul': operators.mul, 'div': operators.truediv, 'mod': operators.mod,
          'in': operators.in_op, 'not_in': operators.not_in_op, 'is': operators.is_, 'is_not': operators.is_not}
FUNCTIONS = {'count', 'sum', 'min', 'max', 'avg', 'coalesce', 'storage_clock', 'storage_after'}


def fields(value, allowed, required=None):
    required = allowed if required is None else required
    if type(value) is not dict or set(value) - set(allowed) or not set(required) <= set(value):
        raise DatabaseError('STORAGE_EXPRESSION_INVALID')
    return value


def datatype(value):
    if isinstance(value, ExactString):
        return {'kind': 'exact_string', 'length': value.length}
    if isinstance(value, TypedJSON):
        return {'kind': 'json'}
    if isinstance(value, UtcDateTime):
        return {'kind': 'datetime'}
    if isinstance(value, Text):
        return {'kind': 'text'}
    if isinstance(value, String):
        return {'kind': 'string', 'length': value.length}
    for cls, name in ((NullType, 'null'), (BigInteger, 'bigint'), (Integer, 'integer'), (Boolean, 'boolean'),
                      (Float, 'float'), (Numeric, 'decimal'), (LargeBinary, 'bytes'), (Date, 'date')):
        if isinstance(value, cls):
            return {'kind': name}
    raise DatabaseError('STORAGE_EXPRESSION_TYPE_UNSUPPORTED')


def restored_type(value):
    fields(value, {'kind', 'length'}, {'kind'})
    kind = value['kind']
    if kind in ('string', 'exact_string'):
        length = value.get('length')
        if length is not None and (type(length) is not int or not 1 <= length <= 1000000):
            raise DatabaseError('STORAGE_EXPRESSION_TYPE_UNSUPPORTED')
        if kind == 'exact_string' and length is None:
            raise DatabaseError('STORAGE_EXPRESSION_TYPE_UNSUPPORTED')
        return ExactString(length) if kind == 'exact_string' else String(length)
    types = {'null': NullType, 'bigint': BigInteger, 'integer': Integer, 'boolean': Boolean, 'float': Float,
             'decimal': Numeric, 'bytes': LargeBinary, 'date': Date, 'text': Text, 'json': TypedJSON,
             'datetime': UtcDateTime}
    if type(kind) is not str or kind not in types or 'length' in value:
        raise DatabaseError('STORAGE_EXPRESSION_TYPE_UNSUPPORTED')
    return types[kind]()


def decoded_value(value):
    result = decode_value(value)
    if type(value) is dict and value.get('type') == 'json':
        return value['value']
    return result


def expression(value, params=None):
    params = {} if params is None else params
    if isinstance(value, BindParameter):
        selected = params.get(value.key, value.value)
        if value.required and value.key not in params:
            raise DatabaseError('STORAGE_EXPRESSION_PARAMETER_REQUIRED')
        if value.expanding:
            return {'node': 'list', 'items': [expression(item) for item in selected]}
        return {'node': 'value', 'value': encode_value(selected), 'type': datatype(value.type)}
    if isinstance(value, ColumnClause):
        if value.table is None or not isinstance(value.table, (Table, Alias)):
            raise DatabaseError('STORAGE_EXPRESSION_COLUMN_UNDECLARED')
        return {'node': 'column', 'table': str(value.table.name), 'name': str(value.name)}
    if isinstance(value, Label):
        return {'node': 'label', 'name': str(value.name), 'expression': expression(value.element, params)}
    if isinstance(value, Grouping):
        return expression(value.element, params)
    if isinstance(value, (True_, False_, Null)):
        return expression(True if isinstance(value, True_) else False if isinstance(value, False_) else None)
    if isinstance(value, BinaryExpression):
        operation = next((key for key, implementation in BINARY.items() if value.operator is implementation), None)
        if operation is None:
            raise DatabaseError('STORAGE_EXPRESSION_OPERATOR_UNSUPPORTED')
        return {'node': 'binary', 'op': operation, 'left': expression(value.left, params),
                'right': expression(value.right, params)}
    if isinstance(value, BooleanClauseList):
        if value.operator not in (operators.and_, operators.or_):
            raise DatabaseError('STORAGE_EXPRESSION_OPERATOR_UNSUPPORTED')
        return {'node': 'boolean', 'op': 'and' if value.operator is operators.and_ else 'or',
                'items': [expression(item, params) for item in value.clauses]}
    if isinstance(value, UnaryExpression):
        selected = value.modifier if value.modifier is not None else value.operator
        kinds = {operators.asc_op: 'asc', operators.desc_op: 'desc', operators.inv: 'not',
                 operators.is_true: 'is_true', operators.is_false: 'is_false', operators.neg: 'neg'}
        if selected not in kinds:
            raise DatabaseError('STORAGE_EXPRESSION_OPERATOR_UNSUPPORTED')
        return {'node': 'unary', 'op': kinds[selected], 'expression': expression(value.element, params)}
    if isinstance(value, Case):
        if value.value is not None:
            raise DatabaseError('STORAGE_EXPRESSION_CASE_UNSUPPORTED')
        return {'node': 'case', 'when': [[expression(condition, params), expression(result, params)]
                                        for condition, result in value.whens],
                'else': expression(value.else_, params)}
    if isinstance(value, FunctionElement):
        if value.name not in FUNCTIONS:
            raise DatabaseError('STORAGE_EXPRESSION_FUNCTION_UNSUPPORTED')
        return {'node': 'function', 'name': value.name,
                'args': [expression(item, params) for item in value.clauses]}
    try:
        return {'node': 'value', 'value': encode_value(value)}
    except (DatabaseError, TypeError, ValueError):
        raise DatabaseError('STORAGE_EXPRESSION_UNSUPPORTED') from None


def source(value, params):
    if isinstance(value, Table):
        return {'node': 'table', 'name': str(value.name)}
    if isinstance(value, Alias) and isinstance(value.original, Table):
        return {'node': 'alias', 'name': str(value.name), 'table': str(value.original.name)}
    if isinstance(value, Join):
        return {'node': 'join', 'left': source(value.left, params), 'right': source(value.right, params),
                'on': expression(value.onclause, params), 'outer': value.isouter, 'full': value.full}
    raise DatabaseError('STORAGE_EXPRESSION_SOURCE_UNSUPPORTED')


def statement(value, params=None):
    params = {} if params is None else params
    if isinstance(value, Select):
        if (value._distinct or value._group_by_clauses or value._having_criteria or value._prefixes
                or value._suffixes or value._fetch_clause is not None or value._correlate or value._correlate_except):
            raise DatabaseError('STORAGE_EXPRESSION_SELECT_UNSUPPORTED')
        lock = value._for_update_arg
        if lock is not None and (lock.nowait or lock.read or lock.of or lock.skip_locked or lock.key_share):
            raise DatabaseError('STORAGE_EXPRESSION_LOCK_UNSUPPORTED')
        return {'kind': 'select', 'columns': [expression(item, params) for item in value.selected_columns],
                'from': [source(item, params) for item in value.get_final_froms()],
                'where': [expression(item, params) for item in value._where_criteria],
                'order': [expression(item, params) for item in value._order_by_clauses],
                'limit': None if value._limit_clause is None else expression(value._limit_clause, params),
                'offset': None if value._offset_clause is None else expression(value._offset_clause, params),
                'lock': lock is not None}
    if isinstance(value, (Update, Delete)):
        result = {'kind': 'update' if isinstance(value, Update) else 'delete', 'table': str(value.table.name),
                  'where': [expression(item, params) for item in value._where_criteria],
                  'returning': [expression(item, params) for item in value._returning]}
        if isinstance(value, Update):
            if getattr(value, '_ordered_values', None) is not None or value._values is None:
                raise DatabaseError('STORAGE_EXPRESSION_UPDATE_UNSUPPORTED')
            result['values'] = {str(key.name if hasattr(key, 'name') else key): expression(item, params)
                                for key, item in value._values.items()}
        return result
    raise DatabaseError('STORAGE_EXPRESSION_STATEMENT_UNSUPPORTED')


def schema_descriptor(package, *, include_digest=True):
    tables = []
    for table in package.metadata.sorted_tables:
        columns = []
        for column in table.columns:
            selected = {'name': str(column.name), 'type': datatype(column.type),
                        'nullable': column.nullable, 'primary_key': column.primary_key}
            if column.server_default is not None:
                selected['default'] = expression(column.server_default.arg)
            columns.append(selected)
        constraints = []
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                constraints.append({'kind': 'unique', 'columns': [str(item.name) for item in constraint.columns]})
            elif isinstance(constraint, ForeignKeyConstraint):
                constraints.append({'kind': 'foreign_key', 'columns': [str(item.name) for item in constraint.columns],
                                    'references': [str(item.target_fullname) for item in constraint.elements]})
            elif isinstance(constraint, CheckConstraint):
                checked = visitors.replacement_traverse(constraint.sqltext, {}, lambda item:
                    table.c[item.name] if isinstance(item, ColumnClause) and item.table is None and item.name in table.c else None)
                constraints.append({'kind': 'check', 'expression': expression(checked)})
        indexes = [{'name': str(index.name), 'unique': index.unique,
                    'expressions': [expression(item) for item in index.expressions]}
                   for index in sorted(table.indexes, key=lambda item: item.name)]
        tables.append({'name': str(table.name), 'columns': columns,
                       'scope': list(table.info.get('scope', ())), 'constraints': constraints, 'indexes': indexes})
    result = {'owner': package.owner, 'version': package.version,
              'appendonly': list(package.appendonly), 'tables': tables}
    if include_digest:
        result['digest'] = package.digest
    return result


def schema_semantics(descriptor):
    fields(descriptor, {'owner', 'version', 'digest', 'appendonly', 'tables'},
           {'owner', 'version', 'appendonly', 'tables'})
    if (type(descriptor['owner']) is not str or not descriptor['owner'] or type(descriptor['version']) is not int
            or descriptor['version'] < 1 or type(descriptor['appendonly']) is not list
            or any(type(name) is not str for name in descriptor['appendonly']) or type(descriptor['tables']) is not list):
        raise DatabaseError('STORAGE_WIRE_SCHEMA_INVALID')

    def canonical(value):
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))

    tables = []
    try:
        for table in descriptor['tables']:
            fields(table, {'name', 'columns', 'scope', 'constraints', 'indexes'})
            if (type(table['name']) is not str or not table['name']
                    or any(type(table[key]) is not list for key in ('columns', 'scope', 'constraints', 'indexes'))
                    or any(type(name) is not str for name in table['scope'])):
                raise DatabaseError('STORAGE_WIRE_SCHEMA_INVALID')
            tables.append({**table, 'scope': sorted(table['scope']),
                           'constraints': sorted(table['constraints'], key=canonical),
                           'indexes': sorted(table['indexes'], key=canonical)})
        return canonical({'owner': descriptor['owner'], 'version': descriptor['version'],
                          'appendonly': sorted(descriptor['appendonly']),
                          'tables': sorted(tables, key=lambda table: table['name'])})
    except (TypeError, ValueError):
        raise DatabaseError('STORAGE_WIRE_SCHEMA_INVALID') from None


class Decoder:
    def __init__(self, metadata):
        self.tables = dict(metadata.tables)

    def table(self, name):
        if type(name) is not str or name not in self.tables:
            raise DatabaseError('STORAGE_EXPRESSION_TABLE_UNDECLARED')
        return self.tables[name]

    def source(self, value):
        if type(value) is not dict:
            raise DatabaseError('STORAGE_EXPRESSION_INVALID')
        node = value.get('node')
        if node == 'table':
            fields(value, {'node', 'name'})
            return self.table(value['name'])
        if node == 'alias':
            fields(value, {'node', 'name', 'table'})
            name = value['name']
            if type(name) is not str or not name.isidentifier() or len(name) > 63 or name in self.tables:
                raise DatabaseError('STORAGE_EXPRESSION_ALIAS_INVALID')
            result = self.table(value['table']).alias(name)
            self.tables[name] = result
            return result
        if node == 'join':
            fields(value, {'node', 'left', 'right', 'on', 'outer', 'full'})
            if type(value['outer']) is not bool or type(value['full']) is not bool:
                raise DatabaseError('STORAGE_EXPRESSION_INVALID')
            left, right = self.source(value['left']), self.source(value['right'])
            return left.join(right, self.expression(value['on']), isouter=value['outer'], full=value['full'])
        raise DatabaseError('STORAGE_EXPRESSION_SOURCE_UNSUPPORTED')

    def expression(self, value, *, native=False):
        if type(value) is not dict:
            raise DatabaseError('STORAGE_EXPRESSION_INVALID')
        node = value.get('node')
        if node == 'value':
            fields(value, {'node', 'value', 'type'}, {'node', 'value'})
            result = decoded_value(value['value'])
            if native:
                return result
            return literal(result, type_=restored_type(value['type']) if 'type' in value else None)
        if node == 'list':
            fields(value, {'node', 'items'})
            return [self.expression(item, native=True) for item in self.items(value['items'])]
        if node == 'column':
            fields(value, {'node', 'table', 'name'})
            table = self.table(value['table'])
            if type(value['name']) is not str or value['name'] not in table.c:
                raise DatabaseError('STORAGE_EXPRESSION_COLUMN_UNDECLARED')
            return table.c[value['name']]
        if node == 'label':
            fields(value, {'node', 'name', 'expression'})
            if type(value['name']) is not str or not value['name'].isidentifier() or len(value['name']) > 128:
                raise DatabaseError('STORAGE_EXPRESSION_LABEL_INVALID')
            return self.expression(value['expression']).label(value['name'])
        if node == 'binary':
            fields(value, {'node', 'op', 'left', 'right'})
            if type(value['op']) is not str or value['op'] not in BINARY:
                raise DatabaseError('STORAGE_EXPRESSION_OPERATOR_UNSUPPORTED')
            right = self.expression(value['right'], native=value['op'] in ('is', 'is_not'))
            return BINARY[value['op']](self.expression(value['left']), right)
        if node == 'boolean':
            fields(value, {'node', 'op', 'items'})
            if value['op'] not in ('and', 'or'):
                raise DatabaseError('STORAGE_EXPRESSION_OPERATOR_UNSUPPORTED')
            values = [self.expression(item) for item in self.items(value['items'])]
            return and_(*values) if value['op'] == 'and' else or_(*values)
        if node == 'unary':
            fields(value, {'node', 'op', 'expression'})
            methods = {'asc': lambda item: item.asc(), 'desc': lambda item: item.desc(), 'not': not_,
                       'neg': lambda item: -item, 'is_true': lambda item: item.is_(True),
                       'is_false': lambda item: item.is_(False)}
            if type(value['op']) is not str or value['op'] not in methods:
                raise DatabaseError('STORAGE_EXPRESSION_OPERATOR_UNSUPPORTED')
            return methods[value['op']](self.expression(value['expression']))
        if node == 'case':
            fields(value, {'node', 'when', 'else'})
            pairs = self.items(value['when'])
            if any(type(pair) is not list or len(pair) != 2 for pair in pairs):
                raise DatabaseError('STORAGE_EXPRESSION_INVALID')
            return case(*[(self.expression(condition), self.expression(result)) for condition, result in pairs],
                        else_=self.expression(value['else']))
        if node == 'function':
            fields(value, {'node', 'name', 'args'})
            name = value['name']
            if type(name) is not str or name not in FUNCTIONS:
                raise DatabaseError('STORAGE_EXPRESSION_FUNCTION_UNSUPPORTED')
            args = [self.expression(item) for item in self.items(value['args'])]
            if name == 'storage_clock':
                if args:
                    raise DatabaseError('STORAGE_EXPRESSION_INVALID')
                return Clock()
            if name == 'storage_after':
                if len(args) != 1:
                    raise DatabaseError('STORAGE_EXPRESSION_INVALID')
                return After(*args)
            return getattr(func, name)(*args)
        raise DatabaseError('STORAGE_EXPRESSION_UNSUPPORTED')

    @staticmethod
    def items(value):
        if type(value) is not list or len(value) > 10000:
            raise DatabaseError('STORAGE_EXPRESSION_INVALID')
        return value

    def values(self, table, values):
        if (type(values) is not dict or any(type(name) is not str or name not in table.c for name in values)):
            raise DatabaseError('STORAGE_EXPRESSION_COLUMN_UNDECLARED')
        return {name: self.expression(value, native=True) for name, value in values.items()}

    def statement(self, value):
        if type(value) is not dict:
            raise DatabaseError('STORAGE_EXPRESSION_INVALID')
        if value.get('kind') == 'select':
            fields(value, {'kind', 'columns', 'from', 'where', 'order', 'limit', 'offset', 'lock'})
            sources = [self.source(item) for item in self.items(value['from'])]
            result = select(*(self.expression(item) for item in self.items(value['columns'])))
            if sources:
                result = result.select_from(*sources)
            result = result.where(*(self.expression(item) for item in self.items(value['where'])))
            result = result.order_by(*(self.expression(item) for item in self.items(value['order'])))
            for name in ('limit', 'offset'):
                if value[name] is not None:
                    count = self.expression(value[name], native=True)
                    if type(count) is not int or not 0 <= count <= 1000000:
                        raise DatabaseError('STORAGE_EXPRESSION_LIMIT_INVALID')
                    result = result.limit(count) if name == 'limit' else result.offset(count)
            if type(value['lock']) is not bool:
                raise DatabaseError('STORAGE_EXPRESSION_INVALID')
            return result.with_for_update() if value['lock'] else result
        if value.get('kind') in ('update', 'delete'):
            keys = {'kind', 'table', 'where', 'returning'} | ({'values'} if value['kind'] == 'update' else set())
            fields(value, keys)
            table = self.table(value['table'])
            result = update(table).values(self.values(table, value['values'])) if value['kind'] == 'update' else delete(table)
            result = result.where(*(self.expression(item) for item in self.items(value['where'])))
            returning = [self.expression(item) for item in self.items(value['returning'])]
            return result.returning(*returning) if returning else result
        raise DatabaseError('STORAGE_EXPRESSION_STATEMENT_UNSUPPORTED')
