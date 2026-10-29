from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .cache import StageCache, StageKey
from .contracts import Suite, digest
from .readonly_stage_cache import ReadThroughStageCache
from .runner import write_json


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verified_suite(directory):
    directory = Path(directory)
    manifest = read(directory / 'manifest.json')
    for name, expected in manifest.items():
        target = (directory / name).resolve()
        if not target.is_relative_to(directory.resolve()) or sha(target) != expected:
            raise ValueError('Historical manifest mismatch: ' + name)
    return Suite.model_validate(read(directory / 'suite.json'))


def source_rows(suite):
    return [dict(id=i, source_id=s.id, kb=s.kb, version=s.version, source_sha256=s.sha256,
                 start=0, end=len(s.text), text=s.text, heading=s.text.splitlines()[0])
            for i, s in enumerate(suite.sources)]


def prepare_locomo(root, output):

    import numpy as np
    from tokenizers import Tokenizer

    history = root / 'reports/raw-memory-20261027/run-01/4000-turn_dense15'
    suite = verified_suite(history)
    protocol = read(history.parent / 'protocol.json')
    dataset = root / '.local/public-agent-v6/research/locomo10.source'
    if sha(dataset) != protocol['dataset_sha256']:
        raise ValueError('LoCoMo dataset hash mismatch')
    pin = read(root / '.local/public-agent-v6/embedding-pin.json')
    prepared = root / '.local/raw-rag-20261027'
    files = read(prepared / 'embedding-preparation.json')['files']
    model = (root / '.local/public-agent-v6/models' /
             ('models--' + pin['repository'].replace('/', '--')) / 'snapshots' / pin['revision'])
    for name, expected in files.items():
        if sha(model / name) != expected:
            raise ValueError('Embedding asset hash mismatch: ' + name)
    tokenizer = Tokenizer.from_file(str(model / 'tokenizer.json'))
    tokenizer.no_padding()
    tokenizer.no_truncation()
    sources = {s.id: s for s in suite.sources}
    rows, semantic = [], []
    for episode in read(dataset):
        conversation = episode['conversation']
        sessions = sorted((int(k.split('_')[1]), k) for k in conversation
                          if k.startswith('session_') and k.split('_')[-1].isdigit())
        for _, session in sessions:
            date = conversation[session + '_date_time']
            for turn in conversation[session]:
                sid = episode['sample_id'] + '/' + turn['dia_id']
                text = turn['speaker'] + ': ' + turn['text']
                if turn.get('blip_caption'):
                    text += '\nImage caption: ' + turn['blip_caption']
                s = sources[sid]
                if s.text != text:
                    raise ValueError('Source metadata/text mismatch')
                offsets = tokenizer.encode(text, add_special_tokens=False).offsets
                for start in range(0, len(offsets), 320):
                    end = min(start + 360, len(offsets))
                    left = 0 if start == 0 else offsets[start][0]
                    right = len(text) if end == len(offsets) else offsets[end - 1][1]
                    chunk = text[left:right]
                    rows.append(dict(id=len(rows), source_id=sid, kb=s.kb, version=s.version,
                                     source_sha256=s.sha256, start=left, end=right, text=chunk, heading=''))
                    semantic.append(' '.join((date, turn['speaker'], '', chunk)))
                    if end == len(offsets):
                        break
    if {r['source_id'] for r in rows} != set(sources):
        raise ValueError('Corpus denominator mismatch')
    def verify(parent, key, payload):
        value = np.asarray(payload, dtype=np.float32)
        if value.shape != (768,) or not np.isfinite(value).all():
            raise ValueError('Invalid cached embedding')
    cache = ReadThroughStageCache(StageCache(output / 'embedding-cache'),
                                 [prepared / 'embedding-stage-cache'], verify_payload=verify)
    base = dict(stage='local_embedding', implementation=digest(files), config_hash=digest(pin),
                corpus_hash=digest(files), index_snapshot='text-addressed-v1', principal='public-regression',
                acl_revision='public-locomo-pinned-v1', model_revision=pin['revision'], sample_id='deterministic-v1')
    def vector(text, query, authorize):
        key = StageKey(**base, upstream=(digest([text, query]),))
        value = cache.get(key, authorize=authorize)
        if value is None:
            raise ValueError('Exact embedding cache miss; no automatic inference')
        return value
    vectors = np.asarray([vector(t, False, lambda r=r: sources[r['source_id']].sha256 == r['source_sha256'])
                          for t, r in zip(semantic, rows, strict=True)], dtype=np.float32)
    queries = {}
    for case in suite.cases:
        req = case.input
        if req.principal != base['principal'] or req.acl_revision != base['acl_revision']:
            raise ValueError('Frozen embedding identity differs')
        queries[req.case_id] = vector(req.question, True,
                                     lambda req=req: set(req.allowed_sources) <= sources.keys())
    np.save(output / 'vectors.npy', vectors, allow_pickle=False)
    write_json(output / 'queries.json', queries)
    write_json(output / 'rows.json', rows)
    write_json(output / 'suite.json', suite.model_dump(mode='json'))
    write_json(output / 'embedding-reuse.json', dict(imported=cache.imported, model=pin,
        model_files=files, preparation_code_sha256=sha(prepared / 'preparation-code.json'),
        historical_suite_sha256=sha(history / 'suite.json'), dataset_sha256=sha(dataset),
        rows=len(rows), queries=len(queries), new_embedding_calls=0,
        boundary='Frozen public ACL; historical inference reused, not re-executed'))
    return suite, rows
