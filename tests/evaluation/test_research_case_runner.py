"""Prove an evaluation run calls the real research CLI and scores its actual saved artifacts.

The existing transport fixture supplies invented Alice/Lab X facts. This proves
runner wiring and independent score handling, NOT real parliamentary accuracy.
No live provider call, DNS lookup or web download is allowed in these tests.
"""
import copy
import io
import json
import unittest
from unittest.mock import patch

from evaluation.research_case_runner import main
from tests.claim_review import test_review_and_discovery_integration as flow_tests
from tests.extraction.extraction_test_fixtures import proposed_batch
from tests.retrieval.retrieval_test_fixtures import public_dns


class ResearchCaseRunnerTests(unittest.TestCase):
    """Exercise free preparation and opt-in run wiring without spending credits."""

    def test_list_and_show_do_not_start_research(self):
        """Reading questions and answer keys must not touch provider credentials or start a graph."""
        with patch('evaluation.research_case_runner.research_main') as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['list']), 0)
            self.assertIn('source_verified | reviewer: assistant', output.getvalue())
            self.assertNotIn('pending', output.getvalue())
            self.assertEqual(main(['show', 'canada_northern_mps']), 0)
            run.assert_not_called()

    def test_live_runner_uses_production_cli_and_keeps_answer_key_out_of_prompt(self):
        """Intercept only external transports, then inspect the runner's actual score and requests."""
        flow = flow_tests.ReviewDiscoveryIntegrationTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        case = {'case_id': 'fixture_membership', 'title': 'Invented membership example',
            'question': 'Identify the current members and represented location of Lab X.',
            'sources': [{'url': 'https://example.org/research', 'kind': 'official'}],
            'expected_members': [{'name': 'Alice', 'aliases': [], 'chamber': 'Lab X', 'region': 'Lab X'}]}
        suite = {'reference_date': '2026-10-22', 'human_reference_review': 'pending',
            'entity_aliases': {}, 'cases': [case], 'private_answer_key_marker': 'DO_NOT_SEND_REFERENCE_ANSWERS'}
        batch = proposed_batch()
        batch['claims'][0]['relationship']['predicate'] = 'MEMBER_OF'
        other = copy.deepcopy(batch['claims'][0])
        other['claim_id'] = 'C2'
        other['relationship']['predicate'] = 'REPRESENTS'
        batch['claims'].append(other)
        flow.overrides['ExtractionBatch'] = lambda body: flow.cli.response(text=json.dumps(batch))
        output = flow.cli.directory / 'evaluations'
        def evaluation_entry(*args, **kwargs):
            """Enter the real evaluation CLI inside the existing offline transport harness."""
            raise SystemExit(main(['run', case['case_id'], '--max-requests', '3', '--max-output-tokens', '1024',
                '--output-dir', str(output)]))
        with patch('evaluation.research_case_runner.load_suite', return_value=suite), \
                patch('runpy.run_module', side_effect=evaluation_entry):
            code, out, err = flow.cli.run_cli(flow.provider, source_dns=public_dns)
        self.assertEqual(code, 0, err)
        score_paths = list(output.glob('*/*/score.json'))
        self.assertEqual(len(score_paths), 1)
        score = json.loads(score_paths[0].read_text())
        self.assertEqual(score['supported_relationships']['correct_count'], 2)
        self.assertEqual(score['usage']['requests_attempted'], 3)
        self.assertEqual(score['reference_validation']['status'], 'not_recorded')
        self.assertEqual(score['human_reference_review'], 'pending')
        self.assertEqual(len(flow.cli.requests), 3)
        for request in flow.cli.requests:
            self.assertNotIn('DO_NOT_SEND_REFERENCE_ANSWERS', request.content.decode())
        self.assertTrue((score_paths[0].parent / 'reference_suite.json').is_file())
        run_directory = next((score_paths[0].parent / 'runs').iterdir())
        with patch('evaluation.research_case_runner.research_main') as run, patch('sys.stdout'):
            self.assertEqual(main(['score', case['case_id'], str(run_directory),
                '--reference-suite', str(score_paths[0].parent / 'reference_suite.json')]), 0)
            run.assert_not_called()
