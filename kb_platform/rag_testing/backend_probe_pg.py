"""ParadeDB BM25 component with the same frozen lexical tokens and field boosts."""
import json

from .contracts import digest


class PgSearchEngine:
    def __init__(self, rows, prefix, tokens, queries):
        import psycopg
        self.conn = psycopg.connect('host=127.0.0.1 port=25432 dbname=probe user=probe password=probe',
                                   autocommit=True)
        self.rows, self.tokens, self.queries = rows, tokens, queries
        self.tables = {kb: prefix + '_' + digest(kb)[:12] for kb in {r['kb'] for r in rows}}

    def build(self):
        from psycopg import sql
        self.conn.execute('CREATE EXTENSION IF NOT EXISTS pg_search')
        self.conn.execute('SET max_parallel_workers_per_gather = 0')
        for kb, name in self.tables.items():
            table = sql.Identifier(name)
            self.conn.execute(sql.SQL('CREATE TABLE {} (rowid bigint PRIMARY KEY, source_id text, '
                                      'body text, exact text, heading text)').format(table))
            values = []
            for row in self.rows:
                if row['kb'] == kb:
                    tokens = self.tokens[str(row['id'])]
                    values.append((row['id'], row['source_id'], *(' '.join(tokens[f])
                                   for f in ('body', 'exact', 'heading'))))
            with self.conn.cursor() as cursor:
                cursor.executemany(sql.SQL('INSERT INTO {} VALUES (%s,%s,%s,%s,%s)').format(table), values)
            config = {f: {'tokenizer': {'type': 'whitespace'}} for f in ('body', 'exact', 'heading')}
            config['source_id'] = {'tokenizer': {'type': 'raw'}}
            self.conn.execute(sql.SQL('CREATE INDEX {} ON {} USING bm25 '
                '(rowid, source_id, body, exact, heading) WITH (key_field={}, text_fields={})').format(
                    sql.Identifier(name + '_bm25'), table, sql.Literal('rowid'), sql.Literal(json.dumps(config))))
            self.conn.execute(sql.SQL('ANALYZE {}').format(table))

    def search(self, request, k, vector=None):
        from psycopg import sql
        if not request.allowed_sources:
            return []
        disjuncts = []
        for field, factor in (('body', 1.0), ('exact', 2.0), ('heading', 1.5)):
            tokens = self.queries[request.case_id]['exact' if field == 'exact' else 'body']
            if tokens:
                disjuncts.append({'boost': {'factor': factor, 'query': {'boolean': {'should': [
                    {'term': {'field': field, 'value': t}} for t in tokens]}}}})
        if not disjuncts:
            return []
        query = {'disjunction_max': {'disjuncts': disjuncts, 'tie_breaker': 0.0}}
        result = self.conn.execute(sql.SQL('SELECT rowid FROM {} WHERE rowid @@@ %s::jsonb '
            'AND source_id = ANY(%s) ORDER BY paradedb.score(rowid) DESC, rowid ASC LIMIT %s').format(
                sql.Identifier(self.tables[request.kb])), (json.dumps(query), list(request.allowed_sources), k))
        return [row[0] for row in result]


class PgVectorEngine:
    def __init__(self, rows, prefix, vectors):
        import psycopg
        self.conn = psycopg.connect('host=127.0.0.1 port=25432 dbname=probe user=probe password=probe',
                                   autocommit=True)
        self.rows, self.vectors = rows, vectors
        self.tables = {kb: prefix + '_' + digest(kb)[:12] for kb in {r['kb'] for r in rows}}
        self.conn.execute('SET hnsw.ef_search = 128')
        self.conn.execute("SET hnsw.iterative_scan = 'strict_order'")

    def build(self):
        from psycopg import sql
        for kb, name in self.tables.items():
            table = sql.Identifier(name)
            self.conn.execute(sql.SQL('CREATE TABLE {} (rowid bigint PRIMARY KEY, source_id text, '
                                      'embedding vector({}))').format(table, sql.Literal(self.vectors.shape[1])))
            values = [(r['id'], r['source_id'], json.dumps(self.vectors[r['id']].tolist()))
                      for r in self.rows if r['kb'] == kb]
            with self.conn.cursor() as cursor:
                cursor.executemany(sql.SQL('INSERT INTO {} VALUES (%s,%s,%s::vector)').format(table), values)
            self.conn.execute(sql.SQL('CREATE INDEX {} ON {} USING hnsw (embedding vector_cosine_ops) '
                'WITH (m=16,ef_construction=100)').format(sql.Identifier(name + '_hnsw'), table))
            self.conn.execute(sql.SQL('ANALYZE {}').format(table))

    def search(self, request, k, vector=None):
        from psycopg import sql
        if not request.allowed_sources:
            return []
        result = self.conn.execute(sql.SQL('SELECT rowid FROM {} WHERE source_id = ANY(%s) '
            'ORDER BY embedding <=> %s::vector LIMIT %s').format(sql.Identifier(self.tables[request.kb])),
            (list(request.allowed_sources), json.dumps(vector), k))
        return [row[0] for row in result]
