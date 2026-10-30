"""Check that the dashboard explains current work using recorded tasks and source IDs.

Small saved-session fixtures exercise the real display-data helper, including
parallel passages and stopped runs. Separate Node.js checks exercise notification
and refresh logic with controlled inputs, not a visually inspected browser or
live research. Inspection must not modify the supplied run.
"""
import copy
import shutil
import subprocess
import unittest
from pathlib import Path

from projectclarity.dashboard.action_context import action_context
from projectclarity.dashboard.projection import TASKS


class ActionContextTests(unittest.TestCase):
    """Keep UI explanations accurate across node transitions and unavailable context."""

    def setUp(self):
        """Create a minimal focused document and competing queued work.

        Args:
            self: Test owner with immutable source display mappings.
        Returns:
            None. The fixture contains only recorded facts needed by the projection.
        """
        self.data = dict(request={'coverage_study': True}, plan={'goals': [
            {'goal_id': 'G1', 'question': 'Who represents each riding?'},
            {'goal_id': 'G2', 'question': 'What coverage is established?'}]}, extraction={
                'attempts': [{'batch_id': 'opaque_batch', 'source_id': 'S4', 'passage_id': 'P8'}],
                'candidates': [{'candidate_id': 'C1', 'proposal': {'goal_ids': ['G1']}}]},
            workflow={'active': 'T1', 'next': 'extract_knowledge', 'tasks': {
                'T1': {'action': 'extract_knowledge', 'status': 'running', 'batch_id': 'opaque_batch'},
                'T2': {'action': 'web_search', 'status': 'blocked', 'blocked': 'cooldown'},
                'T3': {'action': 'retrieve_sources', 'status': 'pending', 'source_id': 'S5'}},
                'document_focus': {'source_id': 'S4', 'status': 'reading', 'candidate_ids': ['C1'],
                                   'remaining_passages': 40}}, coverage_study={'status': 'assessed', 'phase': 'finished'})
        self.sources = {'S4': {'id': 'S4', 'location': 'https://example.org/members'},
                        'S5': {'id': 'S5', 'location': 'https://example.org/other'}}
        self.status = {'worker_running': True, 'active_source_id': 'S99'}
        self.events = [{'event': 'node_started', 'details': {'node_name': 'extract_knowledge'}},
                       {'event': 'extraction_call_started', 'source_id': 'S4',
                        'details': {'batch_id': 'opaque_batch'}}]

    def view(self):
        """Project the saved fixture and assert that inspection is genuinely read-only.

        Args:
            self: Test owner with mutable fixture inputs for each case.
        Returns:
            Display context; fails if the projection changes any engine state.
        """
        before = copy.deepcopy(self.data)
        result = action_context(self.data, self.status, self.events, self.sources, TASKS)
        self.assertEqual(before, self.data)
        return result

    def test_document_passage_linked_goal_and_actual_queue(self):
        """Attribute a passage by its receipt, not an assumed batch-ID format.

        Args:
            self: Focused-document fixture.
        Returns:
            None. Checks source, goal provenance, blocked alternatives and ranking disclaimer.
        """
        value = self.view()
        self.assertEqual(value['source']['id'], 'S4')
        self.assertEqual(value['passage_id'], 'P8')
        self.assertEqual([g['id'] for g in value['goals']], ['G1'])
        self.assertIn('triggered', value['goal_basis'])
        self.assertEqual(sum(t['count'] for t in value['queue']), 3)
        self.assertEqual(next(t for t in value['queue'] if t['action'] == 'web_search')['reason'], 'cooldown')
        self.assertIn('not a ranked shortlist', value['selection_note'])
        self.assertTrue(any('productive-document focus' in x for x in value['policy_context']))

    def test_parallel_passages_are_live_only_and_list_each_source(self):
        """Show every outstanding passage without reviving in-flight labels after stop.

        Args:
            self: Read-only projection fixture with two reserved wire attempts.
        Returns:
            None. Asserts plural activity, source locations and terminal suppression.
        """
        self.data['request']['model_concurrency'] = 3
        self.data['extraction']['attempts'].append(
            {'batch_id': 'other', 'source_id': 'S5', 'passage_id': 'P2'})
        self.data['model_attempts'] = [dict(task='extract_knowledge', call_id=bid, status='started')
            for bid in ('opaque_batch', 'other')]
        view = self.view()
        self.assertEqual(view['title'], 'Reading 2 passages concurrently')
        self.assertEqual([a['location'] for a in view['in_flight']],
            ['https://example.org/members', 'https://example.org/other'])
        self.status['finished'] = True
        self.assertEqual(self.view()['in_flight'], [])

    def test_global_node_does_not_reuse_previous_document_or_goal(self):
        """A later coverage review must not inherit the last extraction's page attribution.

        Args:
            self: Fixture with an intentionally stale monitor source and active reservation.
        Returns:
            None. Current document is unknown and goal basis explicitly names fallback.
        """
        self.events.append({'event': 'node_started', 'details': {'node_name': 'assess_research_gaps'}})
        value = self.view()
        self.assertIsNone(value['source'])
        self.assertIsNone(value['passage_id'])
        self.assertEqual(len(value['goals']), 2)
        self.assertIn('No single active goal', value['goal_basis'])

    def test_scope_stage_and_disabled_scope_remain_distinct(self):
        """Expose the early study even while it is reading a source in a nested stage.

        Args:
            self: Fixture with a running early-study assessment.
        Returns:
            None. Study objective spans goals and disabled study is not called complete.
        """
        self.data['coverage_study'] = {'status': 'running', 'phase': 'assess'}
        value = self.view()
        self.assertTrue(value['studying'])
        self.assertTrue(value['title'].startswith('Researching scope'))
        self.assertEqual(len(value['goals']), 2)
        self.data['coverage_study'] = {}
        self.data['request']['coverage_study'] = False
        value = self.view()
        self.assertFalse(value['study_enabled'])
        self.assertEqual(value['study_status'], 'not_started')

    def test_review_goals_come_from_reserved_claims(self):
        """Prefer this review batch's assignments over a document-focus goal from earlier.

        Args:
            self: Fixture with a second goal explicitly assigned to review.
        Returns:
            None. The UI reports G2 rather than arbitrarily choosing G1.
        """
        self.events.append({'event': 'node_started', 'details': {'node_name': 'review_claims'}})
        self.data['extraction']['candidates'].append({'candidate_id': 'C2', 'proposal': {'goal_ids': ['G2']}})
        self.data['model_attempts'] = [{'status': 'started', 'task': 'review_claims', 'call_id': 'review1'}]
        self.data['workflow']['review_batches'] = {'review1': {'assignments': {'C2': {}}}}
        value = self.view()
        self.assertEqual([g['id'] for g in value['goals']], ['G2'])

    def test_finished_run_has_historical_context_not_live_work(self):
        """Retain the last decision without implying its worker is still running.

        Args:
            self: Fixture marked finished by the actual worker-status contract.
        Returns:
            None. Historical marker remains true even with a stale active-task entry.
        """
        self.status.update(finished=True, worker_running=False)
        self.assertTrue(self.view()['historical'])

    @unittest.skipUnless(shutil.which('node'), 'Node is optional; browser notification regression needs a JS runtime')
    def test_notification_transitions_in_browser_javascript(self):
        """Run the actual notification tracker against replay and status-change cases.

        Args:
            self: Test owner invoking a local JavaScript runtime without networking.
        Returns:
            None. Verifies silent baselines, genuine support changes and no duplicates.
        """
        script = Path(__file__).with_name('notification_checks.js')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node is optional; refresh regression needs a JS runtime')
    def test_idle_and_active_refresh_lifecycle(self):
        """Check production refresh scheduling without launching research or a browser.

        Args:
            self: Test owner invoking deterministic JavaScript transport/timer fixtures.
        Returns:
            None. Idle and hidden states pause; launches, active work and errors recover
            without overlapping requests or losing explicit refresh actions.
        """
        script = Path(__file__).with_name('refresh_checks.js')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
