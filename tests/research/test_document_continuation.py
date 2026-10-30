"""Check which saved findings justify continuing to read a particular document.

Constructed sessions isolate source priority and focus persistence from execution.
Tests reject stale or irrelevant signals, retain useful proposed item fields and
keep periodic other-source reads eligible across restart. No model or network runs;
separate command-line tests exercise these choices inside the workflow.
"""
import copy
import unittest
from types import SimpleNamespace

from projectclarity.nodes.schedule_work.document_continuation import document_work


class DocumentContinuationTests(unittest.TestCase):
    """Keep a scheduling signal narrower than arbitrary mentions or stale evidence."""

    def setUp(self):
        """Build minimal retained receipts for two independent unfinished sources.

        Args:
            self: Unit test case storing synthetic, already-validated receipt shapes.

        Returns:
            None. No model, parsing or evidence-validity claim is made by this fixture.
        """
        self.candidate = {'candidate_id': 'S1_P1_C1', 'source_id': 'S1', 'text_sha256': 'one',
            'evidence_status': 'anchored', 'evidence': [{'quote': 'Retained exact quote.'}],
            'adjudication_status': 'pending',
            'proposal': {'research_roles': ['answer_evidence'], 'goal_ids': ['G1']}}
        self.session = SimpleNamespace(
            documents=[SimpleNamespace(source_id='S1', status='retrieved', text_sha256='one'),
                       SimpleNamespace(source_id='S2', status='retrieved', text_sha256='two')],
            plan=SimpleNamespace(goals=[SimpleNamespace(goal_id='G1')]),
            request=SimpleNamespace(review_claims=False), usage=SimpleNamespace(requests_attempted=2),
            workflow={}, claim_reviews={}, extraction={
                'candidates': [copy.deepcopy(self.candidate)],
                'attempts': [{'source_id': 'S1', 'text_sha256': 'one', 'batch_id': 'S1_P1', 'status': 'completed'},
                             {'source_id': 'S1', 'text_sha256': 'one', 'batch_id': 'S1_P2', 'status': 'not_attempted'},
                             {'source_id': 'S2', 'text_sha256': 'two', 'batch_id': 'S2_P1', 'status': 'not_attempted'}]})
        self.available = [{'action': 'extract_knowledge', 'batch_id': 'S2_P1'},
                          {'action': 'web_search'}, {'action': 'extract_knowledge', 'batch_id': 'S1_P2'}]

    def test_only_anchored_goal_relevant_current_version_answer_signals_qualify(self):
        """Reject stale, invalid, contextual and explicitly rejected candidate signals.

        Args:
            self: Case with independent ready extraction/search work.

        Returns:
            None. Assert invalid signals leave normal scheduling untouched, while
            a valid unreviewed candidate prioritizes its source without a model call.
        """
        cases = [dict(evidence_status='invalid'), dict(evidence=[]), dict(text_sha256='old'),
                 dict(adjudication_status='out_of_scope'), dict(adjudication_status='contradicted'),
                 dict(adjudication_status='insufficient_evidence'),
                 dict(proposal={'research_roles': ['context', 'lead'], 'goal_ids': ['G1']}),
                 dict(proposal={'research_roles': ['answer_evidence'], 'goal_ids': ['UNKNOWN']})]
        for changes in cases:
            with self.subTest(changes=changes):
                self.session.extraction['candidates'] = [{**copy.deepcopy(self.candidate), **changes}]
                self.assertIsNone(document_work(self.session, self.available))
                self.assertEqual(self.session.workflow, {})
        self.session.extraction['candidates'] = [self.candidate]
        self.assertEqual(document_work(self.session, self.available), [self.available[-1]])

    def test_review_rejection_releases_focus_and_preserves_failed_attempt(self):
        """A disproved scheduling signal cannot hold a document indefinitely.

        Args:
            self: Case that first establishes focus and then receives a negative review.

        Returns:
            None. Assert focus release without modifying original passage outcomes.
        """
        document_work(self.session, self.available)
        self.session.extraction['candidates'][0]['adjudication_status'] = 'out_of_scope'
        self.session.extraction['attempts'][0]['status'] = 'failed'
        self.assertIsNone(document_work(self.session, self.available))
        self.assertEqual(self.session.workflow['document_focus']['release_reason'], 'signal_rejected_or_source_changed')
        self.assertEqual(self.session.extraction['attempts'][0]['status'], 'failed')

    def test_existing_focus_survives_earlier_other_signal_and_empty_passage(self):
        """Retain a productive source rather than switching whenever another qualifies.

        Args:
            self: Active source whose next passage completes without another claim.

        Returns:
            None. Assert the saved source retains priority through a newly queued
            refinement and subsequent release records a genuinely completed inventory.
        """
        document_work(self.session, self.available)
        second = {**copy.deepcopy(self.candidate), 'source_id': 'S2', 'text_sha256': 'two',
                  'candidate_id': 'S2_P0_C1'}
        self.session.extraction['candidates'].insert(0, second)
        self.session.extraction['attempts'][1]['status'] = 'completed'
        self.session.extraction['attempts'].append({'source_id': 'S1', 'text_sha256': 'one',
            'batch_id': 'S1_Rfollowup', 'status': 'not_attempted'})
        followup = {'action': 'extract_knowledge', 'batch_id': 'S1_Rfollowup'}
        self.assertEqual(document_work(self.session, [*self.available, followup]), [followup])
        self.session.extraction['attempts'][-1]['status'] = 'completed'
        self.assertEqual(document_work(self.session, self.available), [self.available[0]])
        self.assertEqual(self.session.workflow['document_focus']['source_id'], 'S2')
        self.assertEqual(self.session.workflow['document_focus_history'][0]['release_reason'], 'all_passages_completed')

    def test_blocked_focus_never_reintroduces_unavailable_model_work(self):
        """A priority cannot turn a budget-blocked task into a runnable one.

        Args:
            self: Productive source with its extraction absent from ready work.

        Returns:
            None. Assert the focus is retained as blocked while the caller may
            schedule independent allowed work or terminate with its budget reason.
        """
        self.assertIsNone(document_work(self.session, [{'action': 'retrieve_sources'}]))
        self.assertEqual(self.session.workflow['document_focus']['status'], 'blocked')
        self.assertEqual(self.session.extraction['attempts'][1]['status'], 'not_attempted')

    def test_enumeration_background_is_not_an_item_signal(self):
        """Require item fields before a general answer label can monopolize enumeration.

        Args:
            self: Existing fixture changed to an explicitly shaped enumeration goal.
        Returns:
            None; a population/background claim leaves ordinary scheduling available.
        """
        from projectclarity.research_schemas import Goal
        self.session.plan.goals = [Goal(goal_id='G1', question='List items.',
            evidence_required=['Source records.'], result_shape={'unit': 'items', 'required_fields': ['name']})]
        self.assertIsNone(document_work(self.session, self.available))
        self.assertEqual(self.session.workflow, {})

    def test_explicit_item_lead_can_guide_reading_without_changing_its_verdict(self):
        """A proposed target is worth further reading before it is a supported answer.

        Args:
            self: Shaped goal and anchored, unreviewed lead with actual item fields.
        Returns:
            None; source continuation is permitted without altering evidence or support.
        """
        from projectclarity.research_schemas import Goal
        self.session.plan.goals = [Goal(goal_id='G1', question='List items.',
            evidence_required=['Source records.'], result_shape={'unit': 'items', 'required_fields': ['name']})]
        candidate = self.session.extraction['candidates'][0]
        candidate['proposal'].update(research_roles=['lead'], result_records=[
            {'goal_id': 'G1', 'fields': [{'name': 'name', 'value': 'Possible item'}]}])
        before = copy.deepcopy(candidate)
        self.assertEqual(document_work(self.session, self.available), [self.available[-1]])
        self.assertEqual(candidate, before)

    def test_primary_item_evidence_preempts_secondary_goal_focus(self):
        """Let a sampled primary roster displace a source focused on supporting context.

        Args:
            self: Two source versions and ready tasks with an ordered two-goal plan.
        Returns:
            None; records the reason for switching without discarding the prior queue.
        """
        from projectclarity.research_schemas import Goal
        self.session.plan.goals = [Goal(goal_id='G1', question='List items.',
            evidence_required=['Source records.'], result_shape={'unit': 'items', 'required_fields': ['name']}),
            Goal(goal_id='G2', question='Explain context.', evidence_required=['Context.'])]
        self.session.extraction['candidates'][0]['proposal']['goal_ids'] = ['G2']
        document_work(self.session, self.available)
        other = {**copy.deepcopy(self.candidate), 'source_id': 'S2', 'text_sha256': 'two',
            'candidate_id': 'S2_P0_C1'}
        other['proposal']['result_records'] = [{'goal_id': 'G1', 'fields': [{'name': 'name', 'value': 'New item'}]}]
        self.session.extraction['candidates'].append(other)
        self.assertEqual(document_work(self.session, self.available), [self.available[0]])
        self.assertEqual(self.session.workflow['document_focus']['source_id'], 'S2')
        self.assertEqual(self.session.workflow['document_focus_history'][0]['release_reason'],
                         'higher_priority_item_yield')
        self.assertEqual(self.session.extraction['attempts'][1]['status'], 'not_attempted')

    def test_peer_probe_reservation_survives_restart_without_resetting_service_counts(self):
        """Persist the fairness checkpoint across review turns and a reconstructed session.

        Args:
            self: Active source with four additional completed attempts and an unread peer.
        Returns:
            None; selects the peer once, then returns to retained focus after its completion.
        """
        document_work(self.session, self.available)
        for index in range(4):
            self.session.extraction['attempts'].append({'source_id': 'S1', 'text_sha256': 'one',
                'batch_id': f'S1_Pextra{index}', 'status': 'completed'})
        self.assertEqual(document_work(self.session, self.available), [self.available[0]])
        restored = copy.deepcopy(self.session)
        self.assertEqual(document_work(restored, self.available), [self.available[0]])
        restored.extraction['attempts'][2]['status'] = 'completed'
        self.assertEqual(document_work(restored, self.available[1:]), [self.available[-1]])
        self.assertNotIn('probe_batch_id', restored.workflow['document_focus'])
        self.assertEqual(restored.usage.requests_attempted, self.session.usage.requests_attempted)
