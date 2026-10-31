"""Exercise deterministic reading grants with genuine gain calculations.

Constructed histories test duplicate, negative, unshaped, uncertain and stale
evidence. The existing public-CLI controller suite separately proves production
routing, peer service and persisted diagnostics with simulated provider replies.
Neither suite establishes live semantic usefulness or speed improvements.
"""
import copy
import unittest

from projectclarity.nodes.schedule_work.answer_controller import reading_allowance, choose_work, POLICY
from projectclarity.nodes.schedule_work.source_yield import document_yields
from tests.research import test_source_rotation


class AdaptiveReadingTests(unittest.TestCase):
    """Keep gain-based extensions bounded, reproducible and task-independent."""

    def setUp(self):
        """Prepare actual gain inputs, initially four distinct useful records.

        Args:
            self: Test owning isolated source and extraction histories.
        Returns:
            None; no model, scheduler or evidence calculation is mocked.
        """
        fixture = test_source_rotation.SourceRotationTests()
        fixture.setUp()
        self.session = fixture.session
        self.session.workflow['controller_policy'] = POLICY
        for i, c in enumerate(self.session.extraction['candidates']):
            c['proposal']['result_records'][0]['fields'][0]['value'] = f'Item {i}'

    def allocation(self, used=4):
        """Evaluate the production allocation against current genuine gain inputs.

        Args:
            self: Fixture owner.
            used: Number of attempts already charged to this turn.
        Returns:
            Pure scheduling diagnostic; retained session data stays unchanged.
        """
        return reading_allowance(self.session, 'S1', used, document_yields(self.session))

    def test_productive_extension_is_incremental_and_hard_bounded(self):
        """Require productive extra reads to remain bounded and reproducible.

        Args:
            self: Distinct-record fixture.
        Returns:
            None; each grant is one attempt and stops after eight charged attempts.
        """
        before = copy.deepcopy(self.session)
        for used in range(4, 8):
            self.assertEqual(self.allocation(used)['limit'], used + 1)
        self.assertLessEqual(self.allocation(8)['limit'], 8)
        self.assertEqual(self.session.workflow, before.workflow)
        self.assertEqual(self.allocation(), self.allocation())

    def test_repeated_records_and_late_plateau_do_not_extend(self):
        """Do not let historical productivity hide a newly repetitive tail.

        Args:
            self: Productive fixture whose latest record duplicates an earlier one.
        Returns:
            None; recent zero gain immediately removes the extension.
        """
        self.session.extraction['candidates'][-1]['proposal']['result_records'][0]['fields'][0]['value'] = 'Item 0'
        self.assertEqual(self.allocation()['limit'], 4)

    def test_uncertain_unfinalized_and_stale_reads_do_not_earn_extension(self):
        """Keep failed work and old-source evidence out of positive gain decisions.

        Args:
            self: Fixture varied independently across failure modes.
        Returns:
            None; all uncertain cases fall back to the base turn, not zero-yield truth.
        """
        original = copy.deepcopy(self.session)
        for mode in ['failed', 'unfinalized', 'stale']:
            self.session = copy.deepcopy(original)
            if mode == 'failed':
                self.session.extraction['attempts'][-1]['status'] = 'failed'
            elif mode == 'unfinalized':
                self.session.workflow['finalized_batches'].pop()
            else:
                self.session.documents[0].text_sha256 = 'changed'
            with self.subTest(mode=mode):
                self.assertEqual(self.allocation()['limit'], 4)

    def test_unshaped_explanations_and_counterevidence_can_extend(self):
        """Exercise non-roster gains through the same production calculation.

        Args:
            self: Fixture changed to distinct explanation or counterevidence statements.
        Returns:
            None; useful gains are not restricted to enumeration records.
        """
        for role in ['answer_evidence', 'counterevidence']:
            self.session.plan.goals[0].result_shape = None
            for c in self.session.extraction['candidates']:
                c['proposal']['research_roles'] = [role]
                c['proposal']['result_records'] = []
            self.assertEqual(self.allocation()['limit'], 5)

    def test_old_policy_and_shuffled_inventory_are_stable(self):
        """Retain historical policy and use reservation order, not storage order.

        Args:
            self: Fixture with explicit wire reservation order.
        Returns:
            None; inventory shuffling preserves V2 decisions and V1 stays fixed.
        """
        expected = self.allocation()
        self.session.extraction['attempts'].reverse()
        self.assertEqual(self.allocation(), expected)
        self.session.workflow['controller_policy'] = 'answer-cycle-v1'
        self.assertEqual(self.allocation()['limit'], 4)

    def test_controller_extends_then_services_peer_and_replays_state(self):
        """Exercise actual controller selection, its hard cap and saved-state replay.

        Args:
            self: Productive source with a waiting peer and no pending review work.
        Returns:
            None; one extra read is selected deterministically, then the peer gets
            its sample at the cap. No helper-only allowance can satisfy this test.
        """
        session = self.session
        session.claim_reviews = {}
        session.coverage = {}
        session.workflow['answer_controller'] = dict(phase='read', cycle=1,
            read_start=0, focus_source='S1', last_served={})
        available = []
        for sid in ['S1', 'S2']:
            batch = f'{sid}_next'
            session.extraction['attempts'].append(dict(source_id=sid,
                text_sha256='hash', batch_id=batch, status='not_attempted'))
            available.append(dict(action='extract_knowledge', batch_id=batch))
        restored = copy.deepcopy(session)
        self.assertEqual(choose_work(session, available), [available[0]])
        self.assertEqual(choose_work(restored, available), [available[0]])
        self.assertEqual(session.workflow, restored.workflow)
        for i in range(4):
            session.extraction['attempts'].append(dict(source_id='S1',
                text_sha256='hash', batch_id=f'extra{i}', status='completed'))
        self.assertEqual(choose_work(session, available), [available[1]])
        self.assertEqual(session.workflow['answer_controller']['phase'], 'sample')
