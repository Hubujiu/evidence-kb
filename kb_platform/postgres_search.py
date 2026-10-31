from contextlib import contextmanager
import hashlib
import json
import math

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .search import OpenSearch, SearchUnavailable, binding_key, content_order, lexical_results
from .search_tokens import tokens


FORMAT = 'postgres-textsearch-exact-vector/1'


def lexical_text(text, exact=False):

    return ' '.join('t' + hashlib.sha256(t.encode()).hexdigest() for t in tokens(text, exact=exact))


def validate_vector(vector, dimension):
    if (not isinstance(vector, (list, tuple)) or len(vector) != dimension or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)
            or not any(vector)):
        raise ValueError('Invalid finite nonzero vector')
    return json.dumps(vector, allow_nan=False)


class PostgresSearch:
    index_name = staticmethod(OpenSearch.index_name)
    _index = staticmethod(OpenSearch._index)

    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = ConnectionPool(dsn, min_size=0, max_size=8, timeout=5, open=False,
                                   kwargs={'row_factory': dict_row, 'connect_timeout': 5})

    def close(self):
        self.pool.close()

    @staticmethod
    def _table(index, page=False):
        OpenSearch._index(index)
        return 'g_' + hashlib.md5(index.encode(), usedforsecurity=False).hexdigest() + ('_p' if page else '_b')

    @contextmanager
    def _tx(self, index, tenant=None, kb=None):
        self._index(index)
        try:
            self.pool.open()
            with self.pool.connection() as conn:
                if tenant is None:
                    scope = conn.execute('SELECT * FROM public.search_scope(%s)', (index,)).fetchone()
                    if not scope:
                        raise SearchUnavailable('Search generation not found')
                    tenant, kb = scope['tenant_id'], scope['kb_id']
                conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.kb_id',%s,true)", (tenant, kb))
                yield conn
        except psycopg.Error as exc:
            raise SearchUnavailable('PostgreSQL search operation failed: ' + type(exc).__name__) from exc

    def stage(self, index, units, vector_dimension=None):
        ordered = sorted(units, key=content_order)
        if not ordered or len({u['unit_id'] for u in ordered}) != len(ordered):
            raise SearchUnavailable('Nonempty unique units required')
        tenant, kb = ordered[0]['tenant_id'], ordered[0]['kb_id']
        if any((u['tenant_id'], u['kb_id']) != (tenant, kb) for u in ordered):
            raise SearchUnavailable('Mixed generation scopes')
        checksum = hashlib.sha256(json.dumps(ordered, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        with self._tx(index, tenant, kb) as conn:
            conn.execute('SELECT public.create_search_generation(%s,%s,%s,%s)', (index, vector_dimension, len(ordered), checksum))
            for page in (False, True):
                table = sql.Identifier('kb_search', self._table(index, page))
                query = sql.SQL('INSERT INTO {} VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s)').format(table)
                rows = []
                for i, unit in enumerate(ordered):
                    if (unit['kind'] == 'Page') != page:
                        continue
                    vector = validate_vector(unit['vector'], vector_dimension) if 'vector' in unit else None
                    rows.append((i, unit['unit_id'], tenant, kb, binding_key(unit), unit['kind'], unit['citation_id'],
                        Jsonb(unit), vector, lexical_text(unit['text']), lexical_text(unit['text'], True),
                        lexical_text(unit.get('heading_text', ''))))
                with conn.cursor() as cursor:
                    cursor.executemany(query, rows)
            conn.execute('SELECT public.finish_search_generation(%s)', (index,))

    def verify(self, index):
        with self._tx(index) as conn:
            result = conn.execute('SELECT * FROM public.search_generations WHERE index_name=%s AND ready', (index,)).fetchone()
            if not result:
                raise SearchUnavailable('Search generation is not ready')
            return result

    def read_units(self, index, ids=None):
        with self._tx(index) as conn:
            rows = []
            for page in (False, True):
                query = sql.SQL('SELECT rowid,data FROM {}').format(sql.Identifier('kb_search', self._table(index, page)))
                rows.extend(conn.execute(query if ids is None else query + sql.SQL(' WHERE unit_id=ANY(%s)'),
                                         None if ids is None else (list(ids),)).fetchall())
            ordered = [r['data'] for r in sorted(rows, key=lambda r: r['rowid'])]
            if ids is not None:
                by_id = {r['unit_id']: r for r in ordered}
                return [by_id[i] for i in ids if i in by_id]
            return ordered

    def seal(self, index, checksum):
        with self._tx(index) as conn:
            conn.execute('SELECT public.seal_search_generation(%s,%s)', (index, checksum))

    def is_sealed(self, index, checksum):
        return self.verify(index)['seal'] == checksum

    def publish_manifest(self, manifest, checksum):
        with self._tx(manifest['index_name'], manifest['tenant_id'], manifest['kb_id']) as conn:
            conn.execute('SELECT public.seal_search_generation(%s,%s)', (manifest['index_name'], checksum))
            conn.execute('INSERT INTO public.search_manifests VALUES (%s,%s,%s,%s) '
                'ON CONFLICT (tenant_id,kb_id) DO UPDATE SET manifest=excluded.manifest,checksum=excluded.checksum',
                (manifest['tenant_id'], manifest['kb_id'], Jsonb(manifest), checksum))

    def current_manifest(self, tenant_id, kb_id):
        self.pool.open()
        with self.pool.connection() as conn:
            conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.kb_id',%s,true)", (tenant_id, kb_id))
            row = conn.execute('SELECT manifest,checksum FROM public.search_manifests WHERE tenant_id=%s AND kb_id=%s',
                               (tenant_id, kb_id)).fetchone()
        if not row:
            raise FileNotFoundError('No published semantic manifest')
        return row['manifest'], row['checksum']

    def search(self, index, tenant_id, kb_id, allowed, query, top_k, kind=None, *, exclude_citation_ids=()):
        if not allowed:
            return []
        table = self._table(index, kind == 'Page')
        expressions, params = [], []
        size = max(30, top_k)
        for field, weight in [('body', 1), ('exact', 2), ('heading', 1.5)]:
            field_query = lexical_text(query, field == 'exact')
            if not field_query:
                continue
            predicate = sql.SQL('binding_key=ANY(%s) AND NOT(citation_id=ANY(%s))')
            params += [weight, field_query, 'kb_search.' + table + '_' + field,
                       sorted({binding_key(a) for a in allowed}), list(exclude_citation_ids)]
            if kind:
                predicate += sql.SQL(' AND kind=%s')
                params.append(kind)
            params.append(size)
            expressions.append(sql.SQL('(SELECT rowid,-distance*%s AS score FROM '
                '(SELECT rowid,{} <@> to_bm25query(%s,%s) AS distance FROM {} WHERE {} '
                'ORDER BY distance,rowid LIMIT %s) field_hits)').format(
                    sql.Identifier(field), sql.Identifier('kb_search', table), predicate))
        if not expressions:
            return []

        statement = sql.SQL('WITH ranked AS (SELECT rowid,max(score) AS score FROM ({}) fields '
            'WHERE score>0 GROUP BY rowid ORDER BY score DESC,rowid LIMIT %s) '
            "SELECT u.data - 'vector' AS data,r.score FROM ranked r JOIN {} u USING(rowid) ORDER BY r.score DESC,r.rowid").format(
                sql.SQL(' UNION ALL ').join(expressions), sql.Identifier('kb_search', table))
        params.append(size)
        with self._tx(index, tenant_id, kb_id) as conn:
            rows = conn.execute(statement, params).fetchall()
        hits = [{**r['data'], 'score': r['score']} for r in rows]
        return lexical_results(hits, query, top_k)

    def _dense(self, index, tenant, kb, allowed, vector, top_k, *, pages=False, build=None, excluded=()):
        if not allowed:
            return []
        metadata = self.verify(index)
        query = validate_vector(vector, metadata['dimension'])
        tables = [self._table(index, True)] if pages else [self._table(index, p) for p in (False, True)]
        queries, params = [], []
        for table in tables:
            queries.append(sql.SQL("SELECT data - 'vector' AS data,rowid,embedding <=> %s::vector AS distance FROM {} "
                'WHERE embedding IS NOT NULL AND binding_key=ANY(%s) AND NOT(citation_id=ANY(%s))'
                + (' AND data->>\'build_id\'=%s' if build else '')).format(sql.Identifier('kb_search', table)))
            params.extend([query, sorted({binding_key(a) for a in allowed}), list(excluded)])
            if build:
                params.append(build)
        statement = sql.SQL('SELECT data,1-distance/2 AS score FROM ({}) ranked ORDER BY distance,rowid LIMIT %s').format(
            sql.SQL(' UNION ALL ').join(queries))
        with self._tx(index, tenant, kb) as conn:
            rows = conn.execute(statement, [*params, top_k]).fetchall()
        return [{**r['data'], 'score': r['score']} for r in rows]

    def dense(self, index, tenant_id, kb_id, allowed, vector, top_k):
        return self._dense(index, tenant_id, kb_id, allowed, vector, top_k)

    def dense_pages(self, index, tenant_id, kb_id, build_id, allowed, vector, top_k, excluded):
        return self._dense(index, tenant_id, kb_id, allowed, vector, top_k, pages=True, build=build_id, excluded=excluded)

    def expand(self, index, tenant_id, kb_id, hit, limit=30):
        parent = hit.get('parent_id') or hit['unit_id']
        with self._tx(index, tenant_id, kb_id) as conn:
            rows = conn.execute(sql.SQL("SELECT data - 'vector' AS data FROM {} WHERE binding_key=%s "
                "AND (data->>'parent_id'=%s OR unit_id=ANY(%s)) ORDER BY (data->>'ordinal')::integer,rowid LIMIT %s").format(
                    sql.Identifier('kb_search', self._table(index))), (binding_key(hit), parent, [parent, hit['unit_id']], limit)).fetchall()
        return [{**r['data'], 'score': hit['score']} for r in rows]
