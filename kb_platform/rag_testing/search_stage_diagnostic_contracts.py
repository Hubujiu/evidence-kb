from contextlib import ExitStack
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_platform import embedded_search
from scripts.check_project import source_quality_violations
from . import search_router_contracts, search_stage_diagnostics as diagnostics


class SearchStageDiagnosticContracts(unittest.TestCase):
    def _failure(self, root, error):
        writers = []

        class Index:
            def __init__(self, schema, path):
                self.calls = 0
                writers.append(self)

            def config_reader(self, **kwargs):
                pass

            def writer(self, **kwargs):
                return self

            def commit(self):
                self.calls += 1
                raise error

            def wait_merging_threads(self):
                raise AssertionError('failed commit cannot proceed or retry')

        fixture = search_router_contracts.SearchRouterContracts()
        fixture.root, fixture.units = root, []
        backend = embedded_search.EmbeddedSearch(root / 'lexical-database')
        fixture.router = SimpleNamespace(stage=backend.stage)
        fixture.index = backend.index_name('tenant', 'scope', '1' * 32)
        with patch.object(embedded_search.tv, 'Index', Index):
            try:
                fixture.stage()
            except ValueError as observed:
                self.assertIs(observed, error)
            else:
                self.fail('original exception was suppressed')
        self.assertEqual([writer.calls for writer in writers], [1, 0])
        return error

    def test_first_failure_adds_channel_metadata_without_replacing_exception_or_cause(self):
        cause, error = OSError('synthetic cause'), ValueError('synthetic commit failure')
        error.__cause__ = cause
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._failure(root, error)
            self.assertIs(error.__cause__, cause)
            self.assertEqual(error.args, ('synthetic commit failure',))
            self.assertEqual(len(error.__notes__), 1)
            detail = json.loads(error.__notes__[0].removeprefix(diagnostics.PREFIX))
            self.assertEqual((detail['channel'], detail['status']), ('lexical', 'observed'))
            self.assertTrue(detail['staging'].startswith('lexical-database/.staging-'))
            self.assertNotIn(str(root), error.__notes__[0])
            diagnostics.note_stage_failure(error, root)
            self.assertEqual(len(error.__notes__), 1)

    def test_metadata_rejects_external_roots_symlinks_and_windows_reparse_points(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, 'lstat', side_effect=AssertionError('outside path must not be inspected')):
                self.assertEqual(diagnostics._owned_snapshot(root, root.parent / 'outside'),
                                 {'status': 'outside_owned_root'})
            for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
                info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                with patch.object(Path, 'lstat', return_value=info), \
                        patch.object(diagnostics.os, 'scandir', side_effect=AssertionError('must not follow link')):
                    self.assertEqual(diagnostics._owned_snapshot(root, root / 'staging'),
                                     {'status': 'link_reparse_or_non_directory'})

    def test_metadata_is_bounded_read_only_and_diagnostic_faults_preserve_original_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(diagnostics.LIMIT + 2):
                (root / str(number)).write_bytes(b'synthetic content must not be read')
            with ExitStack() as stack:
                for name in ('open', 'read_text', 'read_bytes'):
                    stack.enter_context(patch.object(Path, name, side_effect=AssertionError('no file content IO')))
                detail = diagnostics._owned_snapshot(root, root)
            self.assertEqual(len(detail['entries']), diagnostics.LIMIT)
            self.assertTrue(detail['truncated'])
            for entry in detail['entries']:
                self.assertEqual(set(entry), {'name', 'size', 'mtime_ns', 'mode', 'attributes', 'blocked'})
                self.assertFalse(Path(entry['name']).is_absolute())
                self.assertNotIn('..', Path(entry['name']).parts)
            error, cause = ValueError('original'), OSError('cause')
            error.__cause__ = cause
            with patch.object(diagnostics, '_owned_snapshot', side_effect=RuntimeError('diagnostic failure')):
                self._failure(root, error)
            self.assertIs(error.__cause__, cause)
            self.assertFalse(hasattr(error, '__notes__'))

    def test_mandatory_source_lint_rejects_retry_early_sampling_and_replacement_mutants(self):
        source = Path(search_router_contracts.__file__).read_text('utf-8')
        call = 'self.router.stage(self.index, self.units, vector_dimension=3)'
        note = 'note_stage_failure(error, self.root)'
        mutants = (source.replace(note, note + '\n            ' + call),
                   source.replace(note + '\n            raise', note + '\n            raise RuntimeError("masked")'),
                   source.replace('        try:\n            ' + call,
                                  '        note_stage_failure(None, self.root)\n        try:\n            ' + call))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'kb_platform/rag_testing/search_router_contracts.py'
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding='utf-8')
            self.assertEqual(source_quality_violations(root), [])
            for mutant in mutants:
                self.assertNotEqual(mutant, source)
                path.write_text(mutant, encoding='utf-8')
                self.assertTrue(any('without retry' in error for error in source_quality_violations(root)))
