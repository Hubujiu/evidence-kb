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
from . import embedded_search_contracts, search_router_contracts, search_stage_diagnostics as diagnostics


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
            with patch.object(diagnostics, '_metadata_file', side_effect=AssertionError('active writers must not read contents')):
                self._failure(root, error)
            self.assertIs(error.__cause__, cause)
            self.assertEqual(error.args, ('synthetic commit failure',))
            self.assertEqual(len(error.__notes__), 1)
            detail = json.loads(error.__notes__[0].removeprefix(diagnostics.PREFIX))
            self.assertEqual((detail['channel'], detail['status']), ('lexical', 'observed'))
            self.assertEqual(detail['metadata'], {'status': 'not_sampled_active_writers'})
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

    def test_embedded_setup_and_generation_loop_keep_original_failure_and_owned_note(self):
        for phase in ('setup', 'generation-loop'):
            with self.subTest(phase=phase):
                fixture = embedded_search_contracts.EmbeddedSearchContracts()
                self.addCleanup(fixture.doCleanups)
                if phase == 'generation-loop':
                    fixture.setUp()
                error, cause, writers = ValueError('synthetic native commit failure'), OSError('synthetic cause'), []
                error.__cause__ = cause

                class Index:
                    def __init__(self, schema, path):
                        self.calls = 0
                        writers.append(self)

                    def config_reader(self, **kwargs):
                        pass

                    def writer(self, **kwargs):
                        return self

                    def add_document(self, document):
                        pass

                    def commit(self):
                        self.calls += 1
                        raise error

                    def wait_merging_threads(self):
                        raise AssertionError('failed commit cannot proceed or retry')

                with patch.object(embedded_search.tv, 'Index', Index):
                    try:
                        if phase == 'setup':
                            fixture.setUp()
                        else:
                            fixture.test_closed_immutable_readers_release_generations_for_immediate_removal()
                    except ValueError as observed:
                        self.assertIs(observed, error)
                    else:
                        self.fail('original exception was suppressed')
                self.assertEqual([writer.calls for writer in writers], [1, 0])
                self.assertIs(error.__cause__, cause)
                self.assertEqual(error.args, ('synthetic native commit failure',))
                self.assertEqual(len(error.__notes__), 1)
                detail = json.loads(error.__notes__[0].removeprefix(diagnostics.PREFIX))
                self.assertEqual((detail['channel'], detail['status']), ('lexical', 'observed'))
                self.assertTrue(detail['staging'].startswith('.staging-'))
                self.assertNotIn(str(Path(fixture.folder.name)), error.__notes__[0])
                fixture.doCleanups()

    def test_real_successful_embedded_setup_and_generation_loop_do_not_sample(self):
        fixture = embedded_search_contracts.EmbeddedSearchContracts()
        self.addCleanup(fixture.doCleanups)
        with patch.object(embedded_search_contracts, 'note_stage_failure',
                          side_effect=AssertionError('successful stage must not sample')) as note:
            fixture.setUp()
            fixture.test_closed_immutable_readers_release_generations_for_immediate_removal()
        note.assert_not_called()

    def test_mandatory_embedded_lint_covers_all_five_call_sites_without_bypass_or_retry(self):
        source = Path(embedded_search_contracts.__file__).read_text('utf-8')
        call = 'return backend.stage(index, units, vector_dimension)'
        note = 'note_stage_failure(error, Path(self.folder.name))'
        mutants = [
            source.replace(call, 'backend.stage(index, units, vector_dimension)\n            ' + call),
            source.replace(note + '\n            raise', note + '\n            raise RuntimeError("masked")'),
            source.replace('        try:\n            ' + call,
                           '        note_stage_failure(None, Path(self.folder.name))\n        try:\n            ' + call),
            source.replace(note, 'note_stage_failure(error, Path(self.folder.name).parent)'),
            source.replace('self._stage(self.backend, self.index, self.authority.units, 3)',
                           'self.backend.stage(self.index, self.authority.units, 3)', 1),
            source.replace('self._stage(backend, index, self.authority.units, 3)',
                           'backend.stage(index, self.authority.units, 3)'),
            source.replace('self._stage(backend, index, self.authority.units, 3)', 'None'),
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'kb_platform/rag_testing/embedded_search_contracts.py'
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding='utf-8')
            self.assertEqual(source_quality_violations(root), [])
            for mutant in mutants:
                self.assertNotEqual(mutant, source)
                path.write_text(mutant, encoding='utf-8')
                self.assertTrue(source_quality_violations(root))
