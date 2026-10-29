"""Immutable, per-build local search. PostgreSQL remains the authorization authority."""
from __future__ import annotations

from contextlib import closing
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')

import numpy as np  # noqa: E402
import tantivy as tv  # noqa: E402

from .search import OpenSearch, SearchUnavailable, binding_key, content_order, lexical_results  # noqa: E402
from .search_tokens import tokens  # noqa: E402


KEYWORDS = ('tenant_id', 'kb_id', 'binding_key', 'kind', 'citation_id', 'build_id')
FIELDS = ('body', 'exact', 'heading', 'page_body', 'page_exact', 'page_heading')
FORMAT = 'tantivy-immutable-exact-vector/3'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class EmbeddedSearch:
    index_name = staticmethod(OpenSearch.index_name)
    _index = staticmethod(OpenSearch._index)

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._readers = OrderedDict()
        self._lock = RLock()

    def close(self):
        with self._lock:
            self._readers.clear()

    def _reader(self, index, channel):
        key = (index, channel)
        with self._lock:
            if key not in self._readers:
                path = self._path(index)
                self.verify(index)
                if channel == 'vectors':
                    value = (np.load(path / 'vectors.npy', mmap_mode='r'),
                             np.load(path / 'vector_ids.npy', allow_pickle=False).tolist())
                else:
                    value = tv.Index.open(str(path / channel))
                self._readers[key] = value
                if len(self._readers) > 32:
                    self._readers.popitem(last=False)
            self._readers.move_to_end(key)
            return self._readers[key]

    def _path(self, index):
        path = self.root / self._index(index)
        if path.resolve().parent != self.root or path.is_symlink():
            raise SearchUnavailable('Invalid index location')
        return path

    def stage(self, index, units, vector_dimension=None):
        destination = self._path(index)
        if destination.exists():
            raise SearchUnavailable('Published index cannot be replaced')
        ordered = sorted(units, key=content_order)
        if len({u['unit_id'] for u in ordered}) != len(ordered):
            raise SearchUnavailable('Duplicate unit identity')
        for unit in ordered:
            if 'vector' in unit:
                value = np.asarray(unit['vector'], dtype=np.float32)
                if value.shape != (vector_dimension,) or not np.isfinite(value).all() or np.linalg.norm(value) == 0:
                    raise SearchUnavailable('Invalid document vector')
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ('.staging-' + uuid4().hex)
        path.mkdir()
        schema = tv.SchemaBuilder()
        schema.add_integer_field('rowid', stored=True, fast=True)
        for field in KEYWORDS:
            schema.add_text_field(field, tokenizer_name='raw')
        for field in FIELDS:
            schema.add_text_field(field, tokenizer_name='whitespace')
        built_schema = schema.build()
        writers = {}
        for name in ('lexical', 'pages'):
            lexical = path / name
            lexical.mkdir()
            idx = tv.Index(built_schema, path=str(lexical))
            writers[name] = idx.writer(heap_size=16_000_000, num_threads=1)
        vectors, ids = [], []
        with closing(sqlite3.connect(path / 'units.sqlite')) as db:
            db.execute('CREATE TABLE units (rowid INTEGER PRIMARY KEY, unit_id TEXT UNIQUE, tenant TEXT, kb TEXT, binding TEXT, kind TEXT, data TEXT)')
            db.execute('CREATE TABLE original_vectors (rowid INTEGER PRIMARY KEY, data TEXT)')
            for rowid, unit in enumerate(ordered):
                key = binding_key(unit)
                db.execute('INSERT INTO units VALUES (?,?,?,?,?,?,?)', (rowid, unit['unit_id'], unit['tenant_id'],
                    unit['kb_id'], key, unit['kind'], _json({k: v for k, v in unit.items() if k != 'vector'})))
                if 'vector' in unit:
                    db.execute('INSERT INTO original_vectors VALUES (?,?)', (rowid, _json(unit['vector'])))
                values = {k: unit.get(k, '') for k in KEYWORDS}
                values['binding_key'] = key
                prefix = 'page_' if unit['kind'] == 'Page' else ''
                values.update({prefix + 'body': ' '.join(tokens(unit['text'])),
                    prefix + 'exact': ' '.join(tokens(unit['text'], exact=True)),
                    prefix + 'heading': ' '.join(tokens(unit.get('heading_text', '')))})
                writers['pages' if prefix else 'lexical'].add_document(tv.Document(rowid=rowid, **values))
                if 'vector' in unit:
                    vector = np.asarray(unit['vector'], dtype=np.float32)
                    if vector.shape != (vector_dimension,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
                        raise SearchUnavailable('Invalid document vector')
                    vectors.append(vector / np.linalg.norm(vector))
                    ids.append(rowid)
            db.execute('CREATE INDEX scope_binding ON units (tenant,kb,binding)')
            db.commit()
        for writer in writers.values():
            writer.commit()
            writer.wait_merging_threads()
        del writer, writers, idx
        np.save(path / 'vectors.npy', np.asarray(vectors, dtype=np.float32).reshape((len(vectors), vector_dimension or 0)))
        np.save(path / 'vector_ids.npy', np.asarray(ids, dtype=np.int64))
        manifest = dict(format=FORMAT, dimension=vector_dimension, count=len(ordered),
            units_sha256=hashlib.sha256(_json(ordered).encode()).hexdigest(),
            files={p.relative_to(path).as_posix(): _sha(p) for p in path.rglob('*') if p.is_file() and not p.name.startswith('.')})
        (path / 'manifest.json').write_text(_json(manifest), encoding='utf-8')
        os.rename(path, destination)

    def verify(self, index):
        path = self._path(index)
        try:
            manifest = json.loads((path / 'manifest.json').read_text('utf-8'))
            if manifest['format'] != FORMAT:
                raise ValueError('format')
            for name, checksum in manifest['files'].items():
                file = path / name
                if not file.resolve().is_relative_to(path) or _sha(file) != checksum:
                    raise ValueError('checksum')
            return manifest
        except (OSError, ValueError, KeyError) as exc:
            raise SearchUnavailable('Local index is unavailable or corrupt') from exc

    def _connection(self, index):
        path = self._path(index) / 'units.sqlite'
        try:
            return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
        except sqlite3.Error as exc:
            raise SearchUnavailable('Local index unavailable; import or rebuild explicitly') from exc

    def read_units(self, index, ids=None):
        with closing(self._connection(index)) as db:
            query = 'SELECT u.data,v.data FROM units u LEFT JOIN original_vectors v ON u.rowid=v.rowid'
            if ids is None:
                rows = db.execute(query + ' ORDER BY u.rowid').fetchall()
            else:
                rows = [row for key in ids if (row := db.execute(query + ' WHERE u.unit_id=?', (key,)).fetchone())]
            return [{**json.loads(data), **({'vector': json.loads(vector)} if vector is not None else {})}
                    for data, vector in rows]

    def seal(self, index, checksum):
        self.verify(index)
        path = self._path(index) / 'seal.json'
        with path.open('x', encoding='utf-8') as stream:
            stream.write(_json(dict(checksum=checksum, manifest=_sha(path.parent / 'manifest.json'))))

    def is_sealed(self, index, checksum):
        try:
            self.verify(index)
            path = self._path(index)
            return json.loads((path / 'seal.json').read_text('utf-8')) == dict(checksum=checksum, manifest=_sha(path / 'manifest.json'))
        except (OSError, ValueError, SearchUnavailable):
            return False

    def search(self, index, tenant_id, kb_id, allowed, query, top_k, kind=None, *, exclude_citation_ids=()):
        if (not isinstance(exclude_citation_ids, (list, tuple)) or len(exclude_citation_ids) > 50 or
                any(not isinstance(v, str) or not v or len(v) > 512 for v in exclude_citation_ids) or
                len(set(exclude_citation_ids)) != len(exclude_citation_ids)):
            raise ValueError('INVALID_EXCLUDED_CITATIONS')
        if not allowed:
            return []
        try:
            idx = self._reader(index, 'pages' if kind == 'Page' else 'lexical')
        except (ValueError, OSError) as exc:
            raise SearchUnavailable('Local lexical index unavailable') from exc
        schema = idx.schema
        prefix = 'page_' if kind == 'Page' else ''
        fields = []
        for field, boost in [('body', 1), ('exact', 2), ('heading', 1.5)]:
            terms = [(tv.Occur.Should, tv.Query.term_query(schema, prefix + field, t))
                     for t in dict.fromkeys(tokens(query, exact=field == 'exact'))]
            if terms:
                fields.append(tv.Query.boost_query(tv.Query.boolean_query(terms), boost))
        if not fields:
            return []
        clauses = [(tv.Occur.Must, tv.Query.disjunction_max_query(fields, 0.0))]
        for field, values in [('tenant_id', [tenant_id]), ('kb_id', [kb_id]),
                              ('binding_key', sorted({binding_key(a) for a in allowed}))]:
            clauses.append((tv.Occur.Must, tv.Query.const_score_query(tv.Query.term_set_query(schema, field, values), 0.0)))
        clauses.append((tv.Occur.Must if kind else tv.Occur.MustNot,
                        tv.Query.const_score_query(tv.Query.term_query(schema, 'kind', kind or 'Page'), 0.0)))
        if exclude_citation_ids:
            clauses.append((tv.Occur.MustNot, tv.Query.term_set_query(schema, 'citation_id', exclude_citation_ids)))
        searcher = idx.searcher()
        expression = tv.Query.boolean_query(clauses)
        size = max(30, top_k)
        limit = size + 1
        while True:
            result = searcher.search(expression, limit=limit)
            if len(result.hits) < limit or result.hits[size - 1][0] > result.hits[-1][0]:
                break
            limit *= 2
        with closing(self._connection(index)) as db:
            hits = [{**json.loads(db.execute('SELECT data FROM units WHERE rowid=?',
                (searcher.doc(address)['rowid'][0],)).fetchone()[0]), 'score': score} for score, address in result.hits]
        for hit in hits:
            hit.pop('vector', None)
        hits.sort(key=lambda h: (-h['score'], content_order(h)))
        return lexical_results(hits[:size], query, top_k)

    def _dense(self, index, tenant, kb, allowed, vector, top_k, *, kind=None, build=None, excluded=()):
        if not allowed:
            return []
        keys = {binding_key(a) for a in allowed}
        with closing(self._connection(index)) as db:
            permitted = {r[0] for r in db.execute('SELECT rowid,binding FROM units WHERE tenant=? AND kb=?', (tenant, kb)) if r[1] in keys}
            vectors, ids = self._reader(index, 'vectors')
            query = np.asarray(vector, dtype=np.float32)
            if query.shape != (vectors.shape[1],) or not np.isfinite(query).all() or np.linalg.norm(query) == 0:
                raise ValueError('Invalid query vector')
            query = query / np.linalg.norm(query)
            candidates = []
            for start in range(0, len(ids), 2048):
                positions = [i for i in range(start, min(start + 2048, len(ids))) if int(ids[i]) in permitted]
                if not positions:
                    continue
                scores = vectors[positions] @ query
                candidates.extend((float(score), int(ids[i])) for i, score in zip(positions, scores, strict=True))
            # Exact filtered ranking: restricted ACLs cannot be starved by global ANN TopK.
            candidates.sort(key=lambda pair: (-pair[0], pair[1]))
            hits = []
            for score, rowid in candidates:
                unit = json.loads(db.execute('SELECT data FROM units WHERE rowid=?', (rowid,)).fetchone()[0])
                if ((kind and unit['kind'] != kind) or (build and unit['build_id'] != build)
                        or unit['citation_id'] in excluded):
                    continue
                unit.pop('vector', None)
                hits.append({**unit, 'score': (score + 1) / 2})
                if len(hits) == top_k:
                    break
            return hits

    def dense(self, index, tenant_id, kb_id, allowed, vector, top_k):
        return self._dense(index, tenant_id, kb_id, allowed, vector, top_k)

    def dense_pages(self, index, tenant_id, kb_id, build_id, allowed, vector, top_k, excluded):
        return self._dense(index, tenant_id, kb_id, allowed, vector, top_k, kind='Page', build=build_id, excluded=excluded)

    def expand(self, index, tenant_id, kb_id, hit, limit=30):
        parent = hit.get('parent_id') or hit['unit_id']
        with closing(self._connection(index)) as db:
            rows = db.execute('SELECT data FROM units WHERE tenant=? AND kb=? AND binding=? ORDER BY rowid',
                              (tenant_id, kb_id, binding_key(hit)))
            result = []
            for row in rows:
                unit = json.loads(row[0])
                if unit.get('parent_id') == parent or unit['unit_id'] in (parent, hit['unit_id']):
                    unit.pop('vector', None)
                    result.append({**unit, 'score': hit['score']})
            return sorted(result, key=lambda u: (u['ordinal'], content_order(u)))[:limit]
