"""Exercise the built-in five-person test through the production terminal entry point and graph.

Only external HTTP/DNS and provider replies are substituted. The actual source
retriever, extractor parser, evidence validator, reviewer, adjudicator, saved
session and evaluator execute. A passing fixture proves cross-source wiring and
test assertions, not that a live LLM will return these answers on current pages.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings
import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from evaluation.membership_reference_cases import get_case, load_suite
from evaluation.research_case_runner import main
from evaluation.multi_source_membership_check import check_membership_result
from tests.claim_review import test_review_and_discovery_integration as flow_tests
from tests.evaluation.multi_source_membership_fixtures import PAGE_MEMBERS, page_and_reply
from tests.retrieval.retrieval_test_fixtures import public_dns


class CombinedMembershipPipelineTests(unittest.TestCase):
    """Keep successful combination, wrong answers and interrupted work observable."""

    def run_combined(self, *, wrong_person=False, budget=20):
        """Run the shipped combined case with four bounded artificial source pages.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            wrong_person: Whether to substitute a wrong member while retaining five distinct
                names, exercising identity checks rather than count alone.
            budget: Maximum model requests allowed for the fixture run, shared by planning and
                subsequent model-backed nodes.

        Returns:
            A tuple of exit code, stdout, stderr, score dictionary, saved session, integration
            harness and selected reference case.
        """
        flow = flow_tests.ReviewDiscoveryIntegrationTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        case = get_case(load_suite(), 'combined_five_members')
        pages = copy.deepcopy(PAGE_MEMBERS)
        if wrong_person:
            pages[3][-1] = ('Patrick Leahy', 'United States Senate', 'Vermont')
        replies = {}
        flow.routes.clear()
        for index, (source, members) in enumerate(zip(case['sources'], pages), 1):
            url = urlsplit(source['url'])
            path = url.path + ('?' + url.query if url.query else '')
            html, replies[f'S{index}'] = page_and_reply(members)
            flow.routes[(url.netloc, '/robots.txt')] = (200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n')
            flow.routes[(url.netloc, path)] = (200, {'Content-Type': 'text/html'}, html)
        flow.cli.plan.update(objective=case['question'], scope_in=['Current federal officeholders in the requested regions'],
            scope_out=['Former members'], assumptions=[], clarifications=[])
        flow.cli.plan['goals'] = [{'goal_id': 'G1', 'question': 'Who currently serves in each chamber and area?',
                                 'evidence_required': ['Current membership and represented area']}]
        flow.cli.plan['strategy'] = [{'action': 'Combine the supplied membership records.',
            'goal_ids': ['G1'], 'source_ids': list(replies), 'proposed_searches': []}]
        flow.cli.plan['completion_criteria'] = [{'criterion_id': 'K1', 'goal_ids': ['G1'],
            'description': 'Identify the current members and their represented areas within the requested scope.',
            'kind': 'enumeration', 'measurement_method': 'Compare distinct current members against source rosters.',
            'target_basis': 'research', 'target_count': None, 'limitations': []}]

        def extraction(body):
            """Return the page's fixed reply, never consult the evaluator's expected names.

            Args:
                body: Decoded provider-request JSON passed by the fixture router; contains the
                    actual serialized task payload.

            Returns:
                A synthetic httpx2.Response passed through the real provider SDK; no network request
                is made by this responder.
            """
            payload = json.loads(body['messages'][0]['content'])
            return flow.cli.response(text=json.dumps(replies[payload['source_id']]))

        def review(body):
            """Supply explicit supported assessments through the normal reviewer adapter.

            Args:
                body: Decoded provider-request JSON passed by the fixture router; contains the
                    actual serialized task payload.

            Returns:
                A synthetic httpx2.Response passed through the real provider SDK; no network request
                is made by this responder.
            Review replies include authored report-mapping judgments; these do not
            establish live semantic recognition accuracy.
            """
            payload = json.loads(body['messages'][0]['content'])
            return flow.cli.response(text=json.dumps({'assessments': [
                {'candidate_id': c['candidate_id'], 'verdict': 'supported', 'in_scope': True,
                 'rationale': 'Synthetic source explicitly states this relationship.',
                 'report_mappings': fixture_mappings(c, payload), 'evidence_quotes': [c['proposal']['evidence'][0]['quote']]}
                for c in payload['candidates']]}))

        flow.overrides.update(ExtractionBatch=extraction, ClaimReviewBatch=review)
        output = flow.cli.directory / 'evaluations'

        def evaluation_entry(*args, **kwargs):
            """Enter the actual evaluation command inside the network-denying SDK harness.

            Args:
                *args: Arguments accepted to match the intercepted runpy entry point; the selected
                    evaluation command comes from the enclosing fixture.
                **kwargs: Runpy keyword arguments accepted for patch compatibility; no alternate
                    evaluator is selected from them.

            Returns:
                Never returns normally; SystemExit carries the actual evaluation terminal interface result to the
                harness.
            """
            raise SystemExit(main(['run', case['case_id'], '--max-requests', str(budget),
                '--output-dir', str(output)]))

        with patch('runpy.run_module', side_effect=evaluation_entry):
            code, out, err = flow.cli.run_cli(flow.provider, source_dns=public_dns)
        score_path, = output.glob('*/*/score.json')
        score = json.loads(score_path.read_text())
        flow.evaluation_score_path = score_path
        session_path, = score_path.parent.glob('runs/*/session.json')
        session = json.loads(session_path.read_text())
        return code, out, err, score, session, flow, case

    def test_four_pages_combine_into_exactly_five_people_and_ten_relations(self):
        """Repeated Balint and alternate Sanders names count once across actual saved artifacts.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, err, score, session, flow, case = self.run_combined()
        self.assertEqual(code, 0, err)
        check = score['acceptance']
        self.assertEqual(check['status'], 'passed')
        self.assertEqual(check['matched_people'], ['Becca Balint', 'Bernie Sanders', 'Brendan Hanley', 'Lori Idlout', 'Peter Welch'])
        self.assertEqual(check['observed_people_count'], 5)
        self.assertEqual(check['contributing_source_count'], 4)
        self.assertEqual(score['supported_relationships']['correct_count'], 10)
        self.assertEqual(len(session['extraction']['candidates']), 12)
        self.assertEqual(len(session['documents']), 4)
        self.assertEqual(len(flow.cli.requests), 6)  # One plan + four extractions + one source-bound review.
        self.assertIn('Built-in multi-source test: PASSED', out)
        for node in ['retrieve_sources', 'extract_knowledge', 'validate_evidence', 'review_claims', 'adjudicate_claims']:
            self.assertIn(node, session['visited_nodes'])
        for request in flow.cli.requests:
            self.assertNotIn('expected_members', request.content.decode())
            self.assertNotIn('minimum_contributing_sources', request.content.decode())
        # Re-scoring uses the same frozen case and must notice subsequent source
        # corruption, even though the saved session still says "supported".
        experiment = flow.evaluation_score_path.parent
        session_path, = (experiment / 'runs').glob('*/session.json')
        run_directory = session_path.parent
        source_file = run_directory / session['documents'][0]['text_path']
        source_file.write_text(source_file.read_text() + '\nChanged after the run.')
        with patch('evaluation.research_case_runner.research_main') as research, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['score', case['case_id'], str(run_directory),
                '--reference-suite', str(experiment / 'reference_suite.json')]), 1)
            rescored = json.loads(output.getvalue())
            self.assertEqual(rescored['acceptance']['status'], 'incomplete')
            self.assertTrue(rescored['source_integrity_errors'])
            research.assert_not_called()

    def test_five_people_with_one_wrong_person_fails_the_real_command(self):
        """Exactly five is insufficient: Leahy replacing Welch must fail with named differences.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, _, score, _, flow, case = self.run_combined(wrong_person=True)
        self.assertEqual(code, 1)
        self.assertEqual(score['research_exit_code'], 0)
        self.assertEqual(score['acceptance']['status'], 'failed')
        self.assertEqual(score['acceptance']['observed_people_count'], 5)
        self.assertEqual(score['acceptance']['missing_people'], ['Peter Welch'])
        self.assertEqual(score['acceptance']['unexpected_people'], ['patrick leahy'])
        self.assertIn('Built-in multi-source test: FAILED', out)
        experiment = flow.evaluation_score_path.parent
        session_path, = (experiment / 'runs').glob('*/session.json')
        run_directory = session_path.parent
        with patch('sys.stdout'), patch('evaluation.research_case_runner.research_main') as research:
            self.assertEqual(main(['score', case['case_id'], str(run_directory),
                '--reference-suite', str(experiment / 'reference_suite.json')]), 1)
            research.assert_not_called()

    def test_budget_stop_is_incomplete_not_a_pass_or_an_incorrect_answer(self):
        """The real request cap leaves receipts which the built-in test must expose.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, _, score, _, flow, _ = self.run_combined(budget=3)
        self.assertEqual(code, 1)
        self.assertEqual(len(flow.cli.requests), 3)
        self.assertEqual(score['acceptance']['status'], 'incomplete')
        self.assertIn('extraction_incomplete', score['acceptance']['incomplete_reasons'])
        self.assertIn('Built-in multi-source test: INCOMPLETE', out)

    def test_fetching_four_pages_does_not_prove_multiple_pages_contributed(self):
        """After a real passing run, falsify provenance alone; the acceptance check must fail.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        _, _, _, score, _, _, case = self.run_combined()
        for candidate in score['scored_candidates']:
            candidate['source_url'] = case['sources'][0]['url']
        check = check_membership_result(case, score)
        self.assertEqual(check['observed_people_count'], 5)
        self.assertEqual(check['status'], 'failed')
        self.assertFalse(check['checks']['multiple_sources_contributed'])
