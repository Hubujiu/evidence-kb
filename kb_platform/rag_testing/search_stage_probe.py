import json
from pathlib import Path
from unittest.mock import patch

from kb_platform import embedded_search
from .product_fixture import Authority
from .search_stage_diagnostics import metadata_counts, note_stage_failure


PROBE_PREFIX = 'embedded-search-controlled-probe: '
EVENT_LIMIT = 12


class StageObservation:
    def __init__(self, root):
        self.root = Path(root)
        self.events = []
        self.case = 'not_started'

    def record(self, phase, path, channel):
        try:
            if len(self.events) < EVENT_LIMIT:
                metadata = metadata_counts(self.root, path, channel) if phase == 'after_wait' else {
                    'status': 'not_sampled_active_writers'}
                self.events.append({'case': self.case, 'phase': phase, 'channel': channel, 'metadata': metadata})
        except BaseException:
            return

    def annotate(self, error):
        try:
            error.add_note(PROBE_PREFIX + json.dumps({'case': self.case, 'events': self.events}, sort_keys=True))
        except BaseException:
            return


class ObservedWriter:
    def __init__(self, writer, observation, path, channel):
        self.writer = writer
        self.observation = observation
        self.path, self.channel = path, channel

    def add_document(self, document):
        return self.writer.add_document(document)

    def commit(self):
        self.observation.record('before_commit', self.path, self.channel)
        result = self.writer.commit()
        self.observation.record('after_commit', self.path, self.channel)
        return result

    def wait_merging_threads(self):
        result = self.writer.wait_merging_threads()
        self.observation.record('after_wait', self.path, self.channel)
        return result


class ObservedIndex:
    def __init__(self, index, observation, path):
        self.index, self.observation = index, observation
        self.path = Path(path)

    def config_reader(self, **kwargs):
        return self.index.config_reader(**kwargs)

    def writer(self, **kwargs):
        return ObservedWriter(self.index.writer(**kwargs), self.observation, self.path.parent, self.path.name)


def single_index_probe(root, observation, with_document):
    path = root / 'lexical'
    path.mkdir(parents=True)
    schema = embedded_search.tv.SchemaBuilder()
    schema.add_text_field('body', stored=True)
    index = embedded_search.tv.Index(schema.build(), path=str(path))
    index.config_reader(reload_policy='Manual')
    writer = ObservedWriter(index.writer(heap_size=16_000_000, num_threads=1), observation, root, 'lexical')
    if with_document:
        writer.add_document(embedded_search.tv.Document(body='fixed synthetic document'))
    writer.commit()
    writer.wait_merging_threads()


def dual_index_probe(root, observation):
    authority = Authority([('one.md', '# Fixed\n\nSynthetic diagnostic input.')])
    authority.units.append({**authority.units[0], 'kind': 'Page', 'unit_id': 'page', 'citation_id': 'page'})
    for unit in authority.units:
        unit['vector'] = [1.0, 1.0, 0.0]
    backend = embedded_search.EmbeddedSearch(root)
    index_name = backend.index_name('tenant', 'scope', '1' * 32)
    original_index = embedded_search.tv.Index

    def observe_index(schema, path):
        return ObservedIndex(original_index(schema, path=path), observation, path)

    try:
        with patch.object(embedded_search.tv, 'Index', observe_index):
            backend.stage(index_name, authority.units, vector_dimension=3)
    except Exception as error:
        note_stage_failure(error, root)
        raise
    finally:
        backend.close()


def run_probes(root, observation):
    try:
        observation.case = 'A_empty'
        single_index_probe(root / 'A', observation, False)
        observation.case = 'B_one_document'
        single_index_probe(root / 'B', observation, True)
        observation.case = 'C_dual_stage'
        dual_index_probe(root / 'C', observation)
    except Exception as error:
        observation.annotate(error)
        raise
    return observation.events
