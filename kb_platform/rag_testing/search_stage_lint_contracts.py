from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.check_project import source_quality_violations
from .formal_state_machines import source_digest


class SearchStageLintContracts(unittest.TestCase):
    def test_shared_formal_fingerprint_keeps_empty_fields_across_supported_python(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'fixture.py'
            path.write_text('def f():\n    pass', encoding='utf-8')
            binding = {'path': 'fixture.py', 'language': 'python', 'symbol': '<module>'}
            expected = '6e896829a45cfcf24fd5414e19676f3e782b0da4dd4d002216ccbc4e9a5d278a'
            self.assertEqual(source_digest(root, binding), expected)
            path.write_text('def f():\n    return 1', encoding='utf-8')
            self.assertNotEqual(source_digest(root, binding), expected)

    def test_mandatory_diagnostic_bindings_reject_behavior_mutants(self):
        sources = {
            'search_stage_probe.py': Path(__file__).with_name('search_stage_probe.py').read_text('utf-8'),
            'search_stage_diagnostics.py': Path(__file__).with_name('search_stage_diagnostics.py').read_text('utf-8'),
            'search_stage_probe_contracts.py': Path(__file__).with_name('search_stage_probe_contracts.py').read_text('utf-8'),
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
