"""Check scoring of final supported report records from a saved research run.

An offline production fixture creates report entries; the test then changes review
status, names and source identity to ensure the scorer catches invalid results.
The reference comparison is separate from whether a simulated provider returned
a successful response. No live model or current-membership verification occurs.
"""
import json
import unittest

from evaluation.research_result_benchmark import score_results
from tests.research import test_result_reporting_cli as reporting_tests


class ResearchResultBenchmarkTests(unittest.TestCase):
    """Score only supported complete pairs, with explicit field mappings and hashes."""

    def test_supported_final_pairs_and_source_integrity(self):
        """Use a real production fixture, then falsify verdicts, names and source identity.

        Args:
            self: Assertion owner; all model replies come from the offline fixture.
        Returns:
            None; verifies wrong pairs/unsupported records cannot receive perfect recall.
        """
        flow = reporting_tests.ResultReportingCLITests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        _, _, _, directory, session, _ = flow.run_case()
        reference = directory.parent / 'expected.json'
        reference.write_text(json.dumps({'source_sha256': session['documents'][0]['raw_sha256'],
            'expected_pairs': [{'mp': 'Aster', 'riding': 'North'}, {'mp': 'Birch', 'riding': 'South'}],
            'excluded_historical_pairs': []}))
        score = score_results(directory, reference, 'name', 'riding')
        result = score['goals'][0]['supported_complete_pairs']
        self.assertEqual((result['correct_count'], result['recall']), (2, 1))
        with self.assertRaises(ValueError):
            score_results(directory, reference, 'invented_field', 'riding')
        for candidate in session['extraction']['candidates']:
            if 'Birch' in candidate['proposal']['statement']:
                candidate['adjudication_status'] = 'insufficient_evidence'
        (directory / 'session.json').write_text(json.dumps(session))
        score = score_results(directory, reference, 'name', 'riding')
        self.assertEqual(score['goals'][0]['supported_complete_pairs']['correct_count'], 1)
        data = json.loads(reference.read_text())
        data['expected_pairs'][0]['riding'] = 'South'
        reference.write_text(json.dumps(data))
        self.assertEqual(score_results(directory, reference, 'name', 'riding')
            ['goals'][0]['supported_complete_pairs']['correct_count'], 0)
        data['source_sha256'] = 'wrong'
        reference.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            score_results(directory, reference, 'name', 'riding')
