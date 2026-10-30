"""Check that research reserves opportunities to assess collected evidence.

A long synthetic source drives the actual command, scheduler and model-call code.
Tests verify periodic assessment with automatic stopping disabled, protection of
the final assessment request, and visible unfinished reviews or failed assessments.
Concurrent extraction and retries must share that same allowance. Provider replies
are controlled; a reserved request is not proof that live assessment succeeds.
"""
import json
import re
import unittest
from unittest.mock import patch

import httpx2

from projectclarity.coverage.freshness import coverage_freshness
from projectclarity.research_schemas import ResearchSession
from tests.research import test_document_continuation_cli as continuation


class CoverageCheckpointCLITests(unittest.TestCase):
    """A productive roster must not monopolize coverage-enabled model budgets."""

    def setUp(self):
        """Create a long, fully productive source and isolated transport harness.

        Args:
            self: Test instance owning temporary sources, receipts and fault switches.
        Returns:
            None. Production parsing, graph, reviews and checkpoint persistence stay real.
        """
        self.fixture = continuation.DocumentContinuationCLITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.install_table(480)
        self.flow = self.fixture.flow
        self.flow.cli.urls.write_text('https://example.org/research\n')
        self.flow.cli.plan['goals'][0]['result_shape'] = {
            'unit': 'researcher', 'required_fields': ['name', 'topic']}
        self.original = self.flow.provider
        self.flow.provider = self.provider
        self.coverage_snapshots = []
        self.crash_coverage = False
        self.malformed_coverage = False
        self.fail_tail = False
        self.fail_extraction = False

    def provider(self, request):
        """Supply complete source-bound records and record genuine assessment boundaries.

        Args:
            self: Fixture with optional crash, malformed-response and retry injection.
            request: Real provider SDK HTTP request produced by the application.
        Returns:
            Synthetic provider response; optional SystemExit simulates a charged,
            uncertain coverage call. No claim or scheduler state is injected directly.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        payload = json.loads(body['messages'][0]['content']) if title != 'ResearchPlan' else {}
        if title == 'CoverageAssessment':
            self.coverage_snapshots.append(self.flow.cli.saved_session()[1])
            if self.crash_coverage:
                self.crash_coverage = False
                raise SystemExit(77)
            if self.malformed_coverage:
                self.malformed_coverage = False
                return self.flow.cli.response(text='{')
        if title == 'ClaimReviewBatch' and self.fail_tail:
            saved = self.flow.cli.saved_session()[1]
            if saved['usage']['requests_attempted'] >= saved['request']['budget']['max_requests'] - 2:
                return httpx2.Response(503, json={'type': 'error',
                    'error': {'type': 'api_error', 'message': 'Synthetic temporary failure'}})
        if title == 'ExtractionBatch':
            passage = payload['passage']
            if self.fail_extraction and passage['passage_id'] == 'P3':
                return httpx2.Response(503, json={'type': 'error',
                    'error': {'type': 'api_error', 'message': 'Synthetic concurrent failure'}})
            pairs = re.findall(r'Name: (Person\d+) \| Topic: (AI security)', passage['text'])
            entities, claims = [], []
            for i, (name, topic) in enumerate(pairs):
                person, subject = f'E{i}', f'T{i}'
                entities.extend([{'entity_id': person, 'name': name, 'entity_type': 'person'},
                                 {'entity_id': subject, 'name': topic, 'entity_type': 'topic'}])
                claims.append({'claim_id': f'C{i}', 'statement': f'{name} researches {topic}.',
                    'goal_ids': ['G1'], 'entity_ids': [person, subject], 'qualifications': [],
                    'research_roles': ['answer_evidence'], 'claim_kind': 'relational',
                    'relationship': {'subject_id': person, 'predicate': 'researches', 'object_id': subject},
                    'result_records': [{'goal_id': 'G1', 'fields': [
                        {'name': 'name', 'value': name}, {'name': 'topic', 'value': topic}]}],
                    'evidence': [{'passage_id': passage['passage_id'],
                                 'quote': f'Name: {name} | Topic: {topic}'}]})
            return self.flow.cli.response(text=json.dumps({'entities': entities, 'claims': claims}))
        return self.original(request)

    def run_case(self, *flags, coverage=True):
        """Run the public command with coverage independent of search and stopping.

        Args:
            self: Case owning transport fixtures and temporary output root.
            *flags: Additional real operator options overriding the defaults below.
            coverage: Whether to explicitly enable periodic coverage assessment.
        Returns:
            Exit code, run directory, saved session and terminal progress output.
            No external source, provider or real credential is contacted.
        """
        options = ['--assess-coverage'] if coverage else []
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'), patch(
                'projectclarity.providers.model_retry.RETRY_BASE_SECONDS', 0):
            return self.flow.run_workflow('--max-requests', '40', '--model-concurrency', '3',
                '--max-no-gain-passages', '0', '--no-stop-when-satisfied', *options, *flags)

    def test_periodic_checks_with_stopping_off_and_fresh_final_coverage(self):
        """Continuous gains cannot starve assessments or spend the reserved final slot.

        Args:
            self: Long productive roster with concurrent extraction and normal reviews.
        Returns:
            None. Assert multiple genuine assessments before source completion,
            current final evidence, retained unread work and exact budget accounting.
        """
        _, _, saved, _ = self.run_case()
        self.assertGreaterEqual(len(self.coverage_snapshots), 2)
        for snap in self.coverage_snapshots:
            self.assertFalse(snap['request']['stop_when_satisfied'])
            self.assertTrue(any(a['status'] == 'not_attempted' for a in snap['extraction']['attempts']))
        calls = saved['model_attempts']
        self.assertEqual(len(calls), 40)
        self.assertEqual(calls[-1]['task'], 'assess_research_gaps')
        self.assertLessEqual(int(next(a for a in calls if a['task'] == 'assess_research_gaps')['attempt_id'][1:]), 17)
        self.assertEqual(saved['workflow']['stop_reason'], 'budget_exhausted')
        self.assertEqual(coverage_freshness(ResearchSession.model_validate(saved))['status'], 'fresh')
        self.assertTrue(all(a['status'] in {'completed', 'not_attempted'} for a in saved['extraction']['attempts']))

    def test_pending_reviews_cannot_consume_last_assessment_opportunity(self):
        """A tight budget permits a qualified assessment without pretending reviews finished.

        Args:
            self: Productive source yielding more claims than one review batch accepts.
        Returns:
            None. Assert retained pending claims, final assessment, and no false stop.
        """
        _, _, saved, _ = self.run_case('--max-requests', '6')
        self.assertEqual(saved['model_attempts'][-1]['task'], 'assess_research_gaps')
        self.assertEqual(len(saved['model_attempts']), 6)
        self.assertTrue(any(c['adjudication_status'] == 'pending' for c in saved['extraction']['candidates']))
        self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
        self.assertEqual(coverage_freshness(ResearchSession.model_validate(saved))['status'], 'fresh')

    def test_coverage_disabled_has_no_reservation_or_assessment(self):
        """Extraction-only runs retain their full allowance without implicit coverage calls.

        Args:
            self: Same long source with coverage not requested.
        Returns:
            None. Assert no coverage dispatch or reserved slot and full budget usage.
        """
        _, _, saved, _ = self.run_case('--max-requests', '7', coverage=False)
        self.assertEqual(len(saved['model_attempts']), 7)
        self.assertFalse(self.coverage_snapshots)
        self.assertFalse(any(t.get('reserve_coverage_call') for t in saved['workflow']['tasks'].values()))

    def test_malformed_assessment_retains_evidence_and_later_checks_continue(self):
        """One invalid coverage response must not stop independent evidence collection.

        Args:
            self: Fixture returning malformed JSON for the first assessment only.
        Returns:
            None. Assert original failure survives and later evidence is assessed.
        """
        self.malformed_coverage = True
        _, _, saved, _ = self.run_case()
        history = saved['coverage']['history']
        self.assertIsNotNone(history[0]['error_code'])
        self.assertEqual(history[-1]['status'], 'assessed')
        self.assertGreater(history[-1]['candidate_total'], history[0]['candidate_total'])

    def test_uncertain_coverage_is_not_replayed_on_resume(self):
        """An interrupted assessment preserves its charge while new evidence proceeds.

        Args:
            self: Case exiting during the first already-reserved coverage wire call.
        Returns:
            None. Assert one planner, one first-coverage attempt and a later final check.
        """
        self.crash_coverage = True
        code, _, before, _ = self.run_case()
        self.assertEqual(code, 77)
        _, saved = self.flow.resume(before['session_id'])
        calls = saved['model_attempts']
        self.assertEqual(sum(c['task'] == 'plan_research' for c in calls), 1)
        self.assertEqual(sum(c['call_id'] == 'coverage_1' for c in calls), 1)
        self.assertEqual(next(c for c in calls if c['call_id'] == 'coverage_1')['status'], 'uncertain')
        self.assertEqual(calls[-1]['task'], 'assess_research_gaps')
        self.assertLessEqual(len(calls), 40)

    def test_transient_review_retries_cannot_spend_final_coverage_slot(self):
        """Review retries after concurrent extraction yield at the reserved boundary.

        Args:
            self: Fixture failing tail review attempts with genuine HTTP 503s.
        Returns:
            None. Assert charged failures remain, retries stop at their effective
            ceiling and a real final coverage response is received within budget.
        """
        self.fail_tail = True
        _, _, saved, _ = self.run_case('--max-requests', '7', '--model-retries', '2')
        calls = saved['model_attempts']
        failed = [c for c in calls if c['status'] == 'failed']
        self.assertTrue(failed)
        self.assertEqual(failed[-1]['recovery_stop'], 'request_limit')
        self.assertEqual(calls[-1]['task'], 'assess_research_gaps')
        self.assertEqual(calls[-1]['status'], 'response_received')
        self.assertEqual(len(calls), 7)
        self.assertEqual(coverage_freshness(ResearchSession.model_validate(saved))['status'], 'fresh')

    def test_failed_final_assessment_is_reported_without_claiming_freshness(self):
        """Reserving capacity must not be confused with a successful final assessment.

        Args:
            self: Small run whose only coverage reply is malformed.
        Returns:
            None. Assert explicit failed coverage, retained findings and no completion.
        """
        self.malformed_coverage = True
        _, _, saved, _ = self.run_case('--max-requests', '6')
        self.assertEqual(saved['model_attempts'][-1]['task'], 'assess_research_gaps')
        self.assertEqual(saved['coverage']['assessment_status'], 'not_assessed')
        self.assertTrue(saved['coverage']['history'][-1]['error_code'])
        self.assertTrue(saved['extraction']['candidates'])
        self.assertNotEqual(coverage_freshness(ResearchSession.model_validate(saved))['status'], 'fresh')

    def test_concurrent_extraction_retry_preserves_peers_and_final_coverage(self):
        """A failing peer cannot consume the final slot or erase successful peers.

        Args:
            self: Three-wide extraction wave with persistent HTTP 503s for passage P3.
        Returns:
            None. Assert failed attempts are charged, completed peers survive and
            the final call assesses the retained evidence within the six-call cap.
        """
        self.fail_extraction = True
        _, _, saved, _ = self.run_case('--max-requests', '6', '--model-retries', '2')
        calls = saved['model_attempts']
        failed = [c for c in calls if c['call_id'] == 'S1_P3']
        self.assertEqual(len(failed), 2)
        self.assertEqual(failed[-1]['recovery_stop'], 'request_limit')
        self.assertEqual(calls[-1]['task'], 'assess_research_gaps')
        self.assertEqual(len(calls), 6)
        attempts = {a['batch_id']: a for a in saved['extraction']['attempts']}
        self.assertEqual(attempts['S1_P2']['status'], 'completed')
        self.assertEqual(attempts['S1_P3']['status'], 'failed')
        self.assertTrue(saved['extraction']['candidates'])
        self.assertEqual(coverage_freshness(ResearchSession.model_validate(saved))['status'], 'fresh')
