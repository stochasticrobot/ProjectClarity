"""Check that grouped model reviews retain every claim and its original source passage.

The public command and research workflow run with simulated service responses.
Cases cover review-size limits, missing or duplicate reply IDs, foreign quotations,
truncation, cancellation and restart. Tests inspect saved claim-to-review accounting
and source checks, not just node visits. The actual SDK parser, scheduler and file
writes execute, but the authored judgments do not measure live review accuracy.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import json
import unittest
from collections import Counter

from tests.claim_review import test_review_and_discovery_integration as review_flow
from tests.research import test_workflow_control_cli as workflow_flow


class BatchedReviewCLITests(unittest.TestCase):
    """Keep every extracted candidate visible through packing, faults and restart."""

    def setUp(self):
        """Install independent page texts and a controllable review HTTP responder.

        Args:
            self: Case owning an isolated terminal interface output directory and synthetic routes.

        Returns:
            None. Real network access is forbidden by the existing terminal interface harness.
        """
        self.flow = review_flow.ReviewDiscoveryIntegrationTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.flow.provider = self.provider
        self.review_inputs = []
        self.fault = None
        self.claim_count = 1
        self.seed_count = 4
        self.order = []
        self.source_padding = ''

    def provider(self, request):
        """Reply to actual SDK calls and inject one batch-level or claim-level fault.

        Args:
            self: Case recording requests and controlling the synthetic failure.
            request: Serialized provider HTTP request from the production adapter.

        Returns:
            Provider-shaped response, or an abrupt SystemExit during review.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        self.order.append(title)
        if title == 'ResearchPlan':
            return self.flow.cli.response()
        payload = json.loads(body['messages'][0]['content'])
        if title == 'ExtractionBatch':
            text = payload['passage']['text']
            quote = next(line for line in text.splitlines() if 'researches attacks.' in line)
            claims = [{'claim_id': f'C{i}', 'statement': f'{quote} Observation {i}.',
                'goal_ids': ['G1'], 'entity_ids': [], 'relationship': None, 'qualifications': [],
                'evidence': [{'passage_id': payload['passage']['passage_id'], 'quote': quote}]}
                for i in range(self.claim_count)]
            return self.flow.cli.response(text=json.dumps({'entities': [], 'claims': claims}))
        self.assertEqual(title, 'ClaimReviewBatch')
        self.review_inputs.append(payload)
        self.assertLessEqual(len(payload['candidates']), 12)
        self.assertLessEqual(len(payload.get('passages', [payload])), 4)
        if self.fault == 'crash':
            self.fault = None
            raise SystemExit(77)
        if self.fault == 'cancel':
            raise KeyboardInterrupt()
        if self.fault == 'truncated':
            return self.flow.cli.response(text='{"assessments":', stop_reason='max_tokens')
        assessments = [{'candidate_id': c['candidate_id'], 'verdict': 'supported',
            'in_scope': True, 'rationale': 'Synthetic source-bound review.',
            'report_mappings': fixture_mappings(c, payload), 'eligibility_assessments': fixture_eligibility(c, payload), 'evidence_quotes': [c['proposal']['evidence'][0]['quote']]} for c in payload['candidates']]
        if self.fault == 'wrong_source':
            assessments[0]['evidence_quotes'] = assessments[1]['evidence_quotes']
        elif self.fault == 'missing':
            assessments.pop(0)
        elif self.fault == 'duplicate':
            assessments[0] = dict(assessments[1])
        elif self.fault == 'unknown':
            assessments[0]['candidate_id'] = 'unknown'
        elif self.fault == 'changed_source':
            directory, _ = self.flow.cli.saved_session()
            (directory / 'documents/S1/text.txt').write_text('Changed source.')
        return self.flow.cli.response(text=json.dumps({'assessments': assessments}))

    def run_case(self, budget=40):
        """Supply distinct seeds, then run actual extraction and review without crawling.

        Args:
            self: Case selecting seed and claim counts.
            budget: Shared model-request ceiling, including initial planning.

        Returns:
            Production terminal interface result tuple with run directory and saved session.
        """
        self.flow.cli.urls.write_text(''.join(f'https://example.org/seed{i}\n' for i in range(self.seed_count)))
        for i in range(self.seed_count):
            self.flow.routes[('example.org', f'/seed{i}')] = (200, {'Content-Type': 'text/plain'},
                (self.source_padding + f'Group {i} researches attacks.').encode())
        return self.flow.run_research(follow=False, budget=budget)

    def assert_accounted(self, saved):
        """Assert the exact candidate-to-review roster and absence of duplicate submissions.

        Args:
            self: Case with recorded actual review input payloads.
            saved: Session loaded from the production run's durable journal.

        Returns:
            None. Fail if any anchored candidate disappears or is submitted twice.
        """
        ids = {c['candidate_id'] for c in saved['extraction']['candidates'] if c['evidence_status'] == 'anchored'}
        self.assertTrue(ids <= saved['claim_reviews'].keys())
        submitted = Counter(c['candidate_id'] for p in self.review_inputs for c in p['candidates'])
        self.assertTrue(all(n == 1 for n in submitted.values()))
        self.assertTrue(set(submitted) <= saved['claim_reviews'].keys())

    def test_four_pages_share_one_review_without_missing_claims(self):
        """Collect four distinct sources before one complete, source-bound review.

        Args:
            self: Case using four single-claim source pages.

        Returns:
            None. Assert six total calls, rather than nine per-passage calls.
        """
        code, _, err, directory, saved = self.run_case()
        self.assertEqual(code, 0, err[-1500:])
        self.assertEqual(saved['usage']['requests_attempted'], 6)
        self.assertEqual(self.order, ['ResearchPlan'] + ['ExtractionBatch'] * 4 + ['ClaimReviewBatch'])
        self.assertEqual(len(self.review_inputs[0]['passages']), 4)
        self.assertEqual({c['adjudication_status'] for c in saved['extraction']['candidates']}, {'supported'})
        self.assert_accounted(saved)
        for candidate_id, review in saved['claim_reviews'].items():
            sid = candidate_id.split('_')[0]
            text = (directory / 'documents' / sid / 'text.txt').read_text()
            for anchor in review['evidence']:
                self.assertEqual(text[anchor['start']:anchor['end']], anchor['quote'])

    def test_large_single_passage_splits_without_loss(self):
        """Split more claims than the response schema allows across successive calls.

        Args:
            self: Case creating 29 claims in one extraction passage.

        Returns:
            None. Assert 12/12/5 packing and exactly one review per candidate.
        """
        self.seed_count, self.claim_count = 1, 29
        code, _, err, directory, saved = self.run_case()
        self.assertEqual(code, 0, err[-1500:])
        self.assertEqual([len(p['candidates']) for p in self.review_inputs], [12, 12, 5])
        self.assertEqual(len(list((directory / 'llm_calls').glob('review_*.json'))), 3)
        self.assert_accounted(saved)

    def test_foreign_source_quote_rejects_only_affected_claim(self):
        """A true quote elsewhere in a batch cannot substantiate the wrong candidate.

        Args:
            self: Case copying the second source's quote into the first assessment.

        Returns:
            None. Assert one failed quote and three retained supported neighbors.
        """
        self.fault = 'wrong_source'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual(saved['claim_reviews']['S1_P1_C1']['error_code'], 'review_quote_not_found')
        self.assertEqual(Counter(c['adjudication_status'] for c in saved['extraction']['candidates']),
            {'needs_review': 1, 'supported': 3})
        self.assert_accounted(saved)

    def test_missing_assessment_remains_explicit_without_losing_neighbors(self):
        """Account for an omitted response ID while preserving valid other assessments.

        Args:
            self: Case dropping the first returned assessment.

        Returns:
            None. Assert exactly one unresolved claim and no silent omission.
        """
        self.fault = 'missing'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual(Counter(c['adjudication_status'] for c in saved['extraction']['candidates']),
            {'needs_review': 1, 'supported': 3})
        self.assert_accounted(saved)

    def test_duplicate_assessment_does_not_approve_ambiguous_ids(self):
        """Reject duplicated and missing IDs, retaining unique neighbors.

        Args:
            self: Case returning the second assessment twice instead of the first.

        Returns:
            None. Assert two unresolved claims and two supported neighbors.
        """
        self.fault = 'duplicate'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual(Counter(c['adjudication_status'] for c in saved['extraction']['candidates']),
            {'needs_review': 2, 'supported': 2})
        self.assert_accounted(saved)

    def test_truncation_retains_the_entire_failed_roster(self):
        """A partial JSON reply never silently certifies or drops a batch subset.

        Args:
            self: Case ending the provider response at its token limit.

        Returns:
            None. Assert every submitted candidate has an incomplete-output receipt.
        """
        self.fault = 'truncated'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual({r['error_code'] for r in saved['claim_reviews'].values()}, {'incomplete_model_output'})
        self.assert_accounted(saved)

    def test_unknown_id_cannot_authorize_any_claim_in_an_invalid_roster(self):
        """Fail closed on a response containing a claim outside the reservation.

        Args:
            self: Case replacing one supplied candidate ID with an invented ID.

        Returns:
            None. Assert all submitted claims remain visible with roster errors.
        """
        self.fault = 'unknown'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual({r['error_code'] for r in saved['claim_reviews'].values()}, {'review_candidate_id_mismatch'})
        self.assert_accounted(saved)

    def test_context_limit_splits_without_clipping_source_passages(self):
        """Keep full long passages while splitting a four-source review by input size.

        Args:
            self: Case with four source texts just below the extraction passage limit.

        Returns:
            None. Assert multiple reviews, complete source contexts and exact accounting.
        """
        self.source_padding = 'x' * 11_700 + '\n'
        code, _, err, directory, saved = self.run_case()
        self.assertEqual(code, 0, err[-1500:])
        self.assertGreater(len(self.review_inputs), 1)
        for payload in self.review_inputs:
            for group in payload.get('passages', [payload]):
                text = (directory / 'documents' / group['source_id'] / 'text.txt').read_text()
                self.assertEqual(group['passage']['text'], text)
        self.assert_accounted(saved)

    def test_cancellation_retains_reserved_and_unsubmitted_claims(self):
        """Cancel a split review while preserving all twenty-nine extracted claims.

        Args:
            self: Case cancelling the first twelve-claim batch inside the model transport.

        Returns:
            None. Assert cancelled receipts for the reserved twelve and pending
            state for the seventeen never submitted to the model.
        """
        self.seed_count, self.claim_count, self.fault = 1, 29, 'cancel'
        _, _, _, _, saved = self.run_case()
        self.assertEqual(saved['status'], 'cancelled')
        self.assertEqual(len(saved['extraction']['candidates']), 29)
        self.assertEqual(len(saved['claim_reviews']), 12)
        self.assertEqual({r['error_code'] for r in saved['claim_reviews'].values()}, {'cancelled'})
        self.assertEqual(Counter(c['adjudication_status'] for c in saved['extraction']['candidates']),
            {'needs_review': 12, 'pending': 17})

    def test_changed_source_cannot_survive_adjudication(self):
        """Recheck original source fingerprints after the batched model response.

        Args:
            self: Case altering one saved source while a review is in flight.

        Returns:
            None. Assert the changed source is invalid, with other sources retained.
        """
        self.fault = 'changed_source'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 1)
        self.assertEqual(Counter(c['adjudication_status'] for c in saved['extraction']['candidates']),
            {'invalid_evidence': 1, 'supported': 3})
        self.assert_accounted(saved)

    def test_low_budget_flushes_before_the_last_extraction(self):
        """Spend the final opportunity on queued review, leaving unread work visible.

        Args:
            self: Case with more pages than its four-call budget can process.

        Returns:
            None. Assert bounded usage, reviewed acquired claims and queued extraction.
        """
        code, _, _, _, saved = self.run_case(budget=4)
        self.assertEqual(code, 1)
        self.assertEqual(saved['usage']['requests_attempted'], 4)
        self.assertTrue(saved['claim_reviews'])
        self.assertTrue(any(t['status'] == 'blocked' for t in saved['workflow']['tasks'].values()))
        self.assertTrue(all(c['adjudication_status'] != 'supported' or c['candidate_id'] in saved['claim_reviews']
            for c in saved['extraction']['candidates']))

    def test_restart_does_not_replay_reserved_batch_or_lose_unsubmitted_claims(self):
        """Reserve twelve of twenty-nine claims, crash, and finish the remaining queue.

        Args:
            self: Case whose first review transport abruptly exits the worker.

        Returns:
            None. Assert twelve uncertain outcomes and seventeen later supported
            claims, without repeating planning, extraction or uncertain review IDs.
        """
        self.seed_count, self.claim_count, self.fault = 1, 29, 'crash'
        code, _, _, _, saved = self.run_case()
        self.assertEqual(code, 77)
        controller = workflow_flow.WorkflowControlCLITests()
        controller.cli, controller.provider = self.flow.cli, self.provider
        code, resumed = controller.resume(saved['session_id'])
        self.assertEqual(code, 1)
        self.assertEqual(self.order.count('ResearchPlan'), 1)
        self.assertEqual(self.order.count('ExtractionBatch'), 1)
        self.assertEqual(Counter(c['adjudication_status'] for c in resumed['extraction']['candidates']),
            {'needs_review': 12, 'supported': 17})
        self.assert_accounted(resumed)

    def test_restart_refreshes_review_file_when_no_new_batch_remains(self):
        """Recover an entirely interrupted review queue without another model call.

        Args:
            self: Four-source case interrupting its only review request.

        Returns:
            None. Assert the standalone review file matches the recovered journal,
            all four failures remain visible and the model-call count is unchanged.
        """
        self.fault = 'crash'
        code, _, _, directory, saved = self.run_case()
        self.assertEqual(code, 77)
        controller = workflow_flow.WorkflowControlCLITests()
        controller.cli, controller.provider = self.flow.cli, self.provider
        _, resumed = controller.resume(saved['session_id'])
        self.assertEqual(resumed['usage']['requests_attempted'], saved['usage']['requests_attempted'])
        self.assertEqual(json.loads((directory / 'claim_reviews.json').read_text())['reviews'], resumed['claim_reviews'])
        self.assertEqual(Counter(c['adjudication_status'] for c in resumed['extraction']['candidates']), {'needs_review': 4})
        self.assert_accounted(resumed)
