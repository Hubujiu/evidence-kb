from pathlib import Path
import tempfile
import unittest

from .backend_probe import fragments_for, freeze_inputs, verify_inputs, summarize
from .backend_probe_data import source_rows
from .backend_probe_report import evidence_gate
from .contracts import AdapterInput, Case, Evidence, Source, Suite, text_hash


class BackendProbeContracts(unittest.TestCase):
    def setUp(self):
        self.source = Source(id='allowed', kb='kb', version='v1', text='approved evidence',
                             sha256=text_hash('approved evidence'))
        self.request = AdapterInput(case_id='q', question='evidence', kb='kb', principal='p',
                                    acl_revision='acl1', allowed_sources=('allowed',),
                                    as_of='2026-10-28', context_budget=100)
        self.suite = Suite(id='synthetic', label_provenance='synthetic_engineering', sources=(self.source,),
            evidence=(Evidence(id='e', source_id='allowed', start=0, end=8, quote='approved', fact='control'),),
            cases=(Case(input=self.request, family='f', any_of=(('e',),)),))

    def test_result_is_bound_to_original_and_permission(self):
        rows = source_rows(self.suite)
        actual = fragments_for([0], rows, self.request, (self.source,))
        self.assertEqual(actual[0].text, 'approved evidence')
        denied = self.request.model_copy(update={'allowed_sources': ()})
        with self.assertRaises(PermissionError):
            fragments_for([0], rows, denied, ())
        rows[0]['text'] = 'invented evidence'
        with self.assertRaises(ValueError):
            fragments_for([0], rows, self.request, (self.source,))

    def test_duplicate_and_invalid_engine_ids_rejected(self):
        for ids in ([0, 0], [-1], [99], [True]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                fragments_for(ids, source_rows(self.suite), self.request, (self.source,))

    def test_frozen_input_changes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'rows.json').write_text('[]')
            freeze_inputs(path)
            verify_inputs(path)
            (path / 'rows.json').write_text('[1]')
            with self.assertRaises(ValueError):
                verify_inputs(path)

    def test_failure_stays_in_evidence_denominator(self):
        report = {'counts': {'ERROR': 1}, 'rows': [{'case_id': 'q', 'outcome': 'ERROR'}]}
        result = summarize(self.suite, report)
        self.assertEqual(result['top20']['denominator'], 1)
        self.assertEqual(result['top20']['complete'], 0)
        self.assertEqual(len(result['errors']), 1)

    def test_equal_aggregate_cannot_hide_per_case_evidence_loss(self):
        baseline = dict(total=2, answerable=2, errors=[], top20=dict(
            complete=1, evidence_by_case={'q1': ['e1'], 'q2': []}))
        candidate = dict(total=2, answerable=2, errors=[], top20=dict(
            complete=1, evidence_by_case={'q1': [], 'q2': ['e2']}))
        gate = evidence_gate(baseline, candidate)
        self.assertFalse(gate['no_evidence_regression'])
        self.assertEqual(gate['lost_evidence']['top20'], {'q1': ['e1']})


if __name__ == '__main__':
    unittest.main()
