import json
from pathlib import Path
import stat
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.check_project import source_quality_violations
from . import search_stage_diagnostics as diagnostics, search_stage_probe as probe


class SearchStageProbeContracts(unittest.TestCase):
    def test_real_three_case_single_pass_retains_stage_observations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            observation = probe.StageObservation(root)
            events = probe.run_probes(root, observation)
            self.assertEqual(len(events), 12)
            self.assertEqual([event['case'] for event in events],
                             ['A_empty'] * 3 + ['B_one_document'] * 3 + ['C_dual_stage'] * 6)
            self.assertEqual([event['phase'] for event in events],
                             ['before_commit', 'after_commit', 'after_wait'] * 4)
            self.assertEqual([event['channel'] for event in events], ['lexical'] * 9 + ['pages'] * 3)
            encoded = json.dumps({'events': events}, sort_keys=True)
            self.assertNotIn(str(root), encoded)
            print(probe.PROBE_PREFIX + encoded)

    def test_only_completed_channel_is_read_after_successful_wait(self):
        observation = probe.StageObservation(Path('owned'))
        with patch.object(probe, 'metadata_counts', return_value={'status': 'synthetic'}) as sample:
            observation.record('before_commit', Path('owned'), 'lexical')
            observation.record('after_commit', Path('owned'), 'lexical')
            sample.assert_not_called()
            observation.record('after_wait', Path('owned'), 'lexical')
            sample.assert_called_once_with(Path('owned'), Path('owned'), 'lexical')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'lexical').mkdir()
            (root / 'pages').mkdir()
            with patch.object(diagnostics, '_metadata_file', return_value={'status': 'synthetic'}) as read:
                diagnostics.metadata_counts(root, root, 'lexical')
            self.assertEqual([call.args[0] for call in read.call_args_list],
                             [root / 'lexical' / 'meta.json', root / 'lexical' / '.managed.json'])

    def test_first_case_failure_stops_later_cases_with_original_error(self):
        for failure_case in ('A_empty', 'B_one_document', 'C_dual_stage'):
            with self.subTest(case=failure_case), TemporaryDirectory() as directory:
                root = Path(directory)
                observation = probe.StageObservation(root)
                cause, error, calls = OSError('original cause'), ValueError('original failure'), []
                error.__cause__ = cause

                def invoke(*args):
                    calls.append(observation.case)
                    if observation.case == failure_case:
                        raise error

                with patch.object(probe, 'single_index_probe', side_effect=invoke), \
                        patch.object(probe, 'dual_index_probe', side_effect=invoke):
                    with self.assertRaises(ValueError) as caught:
                        probe.run_probes(root, observation)
                self.assertIs(caught.exception, error)
                self.assertIs(error.__cause__, cause)
                self.assertEqual(error.args, ('original failure',))
                self.assertEqual(calls, ['A_empty', 'B_one_document', 'C_dual_stage'][:len(calls)])
                self.assertEqual(calls[-1], failure_case)
                self.assertEqual(len(error.__notes__), 1)

    def test_native_failure_does_not_wait_or_record_success(self):
        class Writer:
            def commit(self):
                raise error

            def wait_merging_threads(self):
                raise AssertionError('must not wait after commit failed')

        error = ValueError('original native error')
        observation = probe.StageObservation(Path('owned'))
        with patch.object(probe, 'metadata_counts', return_value={'status': 'synthetic'}):
            writer = probe.ObservedWriter(Writer(), observation, Path('owned'), 'lexical')
            with self.assertRaises(ValueError) as caught:
                writer.commit()
        self.assertIs(caught.exception, error)
        self.assertEqual([event['phase'] for event in observation.events], ['before_commit'])

    def test_diagnostic_failure_and_capacity_never_mask_writer_result(self):
        calls = []

        class Writer:
            def commit(self):
                calls.append('commit')
                return 7

            def wait_merging_threads(self):
                calls.append('wait')
                return None

        observation = probe.StageObservation(Path('owned'))
        writer = probe.ObservedWriter(Writer(), observation, Path('owned'), 'lexical')
        with patch.object(probe, 'metadata_counts', side_effect=OSError('diagnostic failure')):
            self.assertEqual(writer.commit(), 7)
            self.assertIsNone(writer.wait_merging_threads())
        self.assertEqual(calls, ['commit', 'wait'])
        self.assertEqual([event['phase'] for event in observation.events], ['before_commit', 'after_commit'])
        with patch.object(probe, 'metadata_counts', return_value={'status': 'synthetic'}):
            for _ in range(probe.EVENT_LIMIT + 2):
                observation.record('before_commit', Path('owned'), 'lexical')
        self.assertEqual(len(observation.events), probe.EVENT_LIMIT)

    def test_allowlisted_metadata_outputs_counts_not_contents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            channel = root / 'lexical'
            channel.mkdir()
            (channel / 'meta.json').write_text(json.dumps({'opstamp': 8, 'segments': [{'secret': 'must not appear'}],
                                                         'schema': 'private schema'}), encoding='utf-8')
            (channel / '.managed.json').write_text('["private segment name"]', encoding='utf-8')
            summary = diagnostics.metadata_counts(root, root, 'lexical')
            self.assertEqual(summary['files']['meta.json'],
                             {'status': 'observed', 'opstamp': 8, 'segment_count': 1})
            self.assertEqual(summary['files']['.managed.json'],
                             {'status': 'observed', 'managed_count': 1})
            encoded = json.dumps(summary)
            for secret in ('must not appear', 'private schema', 'private segment name', str(root)):
                self.assertNotIn(secret, encoded)

    def test_metadata_limit_format_unavailable_and_name_guards(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            channel = root / 'lexical'
            channel.mkdir()
            path = channel / 'meta.json'
            path.write_bytes(b'x' * (diagnostics.METADATA_LIMIT + 1))
            with patch.object(diagnostics.os, 'open', side_effect=AssertionError('oversized file must not open')):
                self.assertEqual(diagnostics._metadata_file(path), {'status': 'oversized'})
                self.assertEqual(diagnostics._metadata_file(channel / 'document.json'), {'status': 'not_allowlisted'})
            path.write_text('{}', encoding='utf-8')
            self.assertEqual(diagnostics._metadata_file(path), {'status': 'unexpected_format'})
            path.write_text('{', encoding='utf-8')
            self.assertEqual(diagnostics._metadata_file(path), {'status': 'unavailable'})
            with patch.object(diagnostics.os, 'open', side_effect=PermissionError('synthetic denial')):
                self.assertEqual(diagnostics._metadata_file(path), {'status': 'unavailable'})
            self.assertEqual(diagnostics.metadata_counts(root, root.parent, 'lexical'), {'status': 'outside_owned_root'})

    def test_metadata_identity_changes_and_reparse_files_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'lexical' / 'meta.json'
            path.parent.mkdir()
            path.write_text('{"opstamp": 0, "segments": []}', encoding='utf-8')
            before = path.lstat()
            changed = SimpleNamespace(st_dev=before.st_dev, st_ino=before.st_ino + 1,
                                      st_size=before.st_size, st_mtime_ns=before.st_mtime_ns, st_mode=before.st_mode)
            with patch.object(diagnostics.os, 'fstat', return_value=changed):
                self.assertEqual(diagnostics._metadata_file(path), {'status': 'changed'})
            with patch.object(diagnostics.os, 'fstat', side_effect=[before, changed]):
                self.assertEqual(diagnostics._metadata_file(path), {'status': 'changed'})
            for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
                info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                with patch.object(Path, 'lstat', return_value=info), \
                        patch.object(diagnostics.os, 'open', side_effect=AssertionError('must not open link')):
                    self.assertEqual(diagnostics._metadata_file(path), {'status': 'link_reparse_or_non_regular'})

    def test_wait_failure_retains_committed_observation_and_original_error(self):
        error, cause, calls = ValueError('wait failure'), OSError('cause'), []
        error.__cause__ = cause

        class Writer:
            def commit(self):
                calls.append('commit')
                return 1

            def wait_merging_threads(self):
                calls.append('wait')
                raise error

        observation = probe.StageObservation(Path('owned'))
        writer = probe.ObservedWriter(Writer(), observation, Path('owned'), 'lexical')
        with patch.object(probe, 'metadata_counts', return_value={'status': 'synthetic'}):
            writer.commit()
            with self.assertRaises(ValueError) as caught:
                writer.wait_merging_threads()
        self.assertIs(caught.exception, error)
        self.assertIs(error.__cause__, cause)
        self.assertEqual(error.args, ('wait failure',))
        self.assertEqual(calls, ['commit', 'wait'])
        self.assertEqual([event['phase'] for event in observation.events], ['before_commit', 'after_commit'])

    def test_failed_annotation_cannot_replace_first_case_error(self):
        class Failure(ValueError):
            def add_note(self, note):
                raise OSError('annotation unavailable')

        error = Failure('native error')
        observation = probe.StageObservation(Path('owned'))
        with patch.object(probe, 'single_index_probe', side_effect=error) as single, \
                patch.object(probe, 'dual_index_probe', side_effect=AssertionError('must stop')) as dual:
            with self.assertRaises(Failure) as caught:
                probe.run_probes(Path('owned'), observation)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.args, ('native error',))
        self.assertEqual(single.call_count, 1)
        dual.assert_not_called()

    def test_mandatory_diagnostic_bindings_reject_behavior_mutants(self):
        sources = {
            'search_stage_probe.py': Path(probe.__file__).read_text('utf-8'),
            'search_stage_diagnostics.py': Path(diagnostics.__file__).read_text('utf-8'),
            'search_stage_probe_contracts.py': Path(__file__).read_text('utf-8'),
        }
        replacements = {
            'search_stage_probe_contracts.py': (
                ('events = probe.run_probes(root, observation)', 'events = []'),
            ),
            'search_stage_probe.py': (
                ('result = self.writer.commit()', 'self.writer.commit()\n        result = self.writer.commit()'),
                ('observation.annotate(error)\n        raise', 'observation.annotate(error)\n        return []'),
                ("single_index_probe(root / 'A', observation, False)", 'None'),
                ('EVENT_LIMIT = 12', 'EVENT_LIMIT = 120'),
                ("'after_commit'", "'before_commit'"),
                ("phase == 'after_wait'", "phase == 'before_commit'"),
                ("metadata_counts(self.root, path, channel)", "metadata_counts(self.root, path, 'pages')"),
            ),
            'search_stage_diagnostics.py': (
                ('METADATA_LIMIT = 65536', 'METADATA_LIMIT = 99999999'),
                ('stream.read(METADATA_LIMIT + 1)', 'stream.read()'),
                ("('meta.json', '.managed.json')", "('meta.json', '.managed.json', 'document.json')"),
                ("'segment_count': len(value['segments'])", "'segment_count': value['segments']"),
                ('_identity(opened) != _identity(before)', 'False'),
                ("summary = {'status': 'not_sampled_active_writers'}", "summary = metadata_counts(root, path, channel)"),
                ('_blocked(info) or not stat.S_ISDIR(info.st_mode)', 'False'),
            ),
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'kb_platform/rag_testing'
            folder.mkdir(parents=True)
            for filename, source in sources.items():
                path = folder / filename
                path.write_text(source, encoding='utf-8')
                self.assertEqual(source_quality_violations(root), [])
                for old, new in replacements[filename]:
                    mutant = source.replace(old, new)
                    self.assertNotEqual(source, mutant)
                    path.write_text(mutant, encoding='utf-8')
                    self.assertTrue(source_quality_violations(root))
                path.write_text(source, encoding='utf-8')
