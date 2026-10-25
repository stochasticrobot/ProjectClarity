"""Failure-first scheduler tests; no model/network operation is replaced by success.

These unit tests inspect journal decisions. test_workflow_control_cli separately
exercises the actual entry point, adapters, parser, retrieval and saved artifacts.
"""
import tempfile
import unittest
from pathlib import Path

from projectclarity.discovery.followup_selection_contracts import FollowupChoice
from projectclarity.discovery.selection_acceptance import accept_choices, pending_offered_ids
from projectclarity.research_schemas import FileSource, ResearchSession, RetrievedDocument, URLSource
from projectclarity.nodes.schedule_work.workflow_control import schedule_work, work_inventory, settle_work
from tests.research.research_test_fixtures import example_session


class WorkflowControlTests(unittest.TestCase):
    """Verify work conservation, bounded recovery, fair dispatch and durable decisions."""

    def setUp(self):
        """Create a minimal planned run with two independently runnable acquisitions.

        Args:
            self: Test owner of an isolated journal directory and typed session.

        Returns:
            None. No provider, source, checkpoint or live network is accessed.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.session = example_session(plan_only=False)
        self.session.sources.append(URLSource(source_id='S2', url='https://example.org/other'))
        (self.root / self.session.session_id).mkdir()

    def dispatch(self):
        """Schedule the next unit and round-trip JSON to exercise durable state semantics.

        Args:
            self: Test owner holding the current run and its saved directory.

        Returns:
            The active task dictionary, or None when the scheduler waits/stops.
        """
        schedule_work({'session': self.session}, self.root)
        self.session = ResearchSession.model_validate_json(
            (self.root / self.session.session_id / 'session.json').read_text())
        return self.session.workflow['tasks'].get(self.session.workflow.get('active'))

    def document(self, source_id, error=None):
        """Supply an explicit acquisition outcome for scheduler-only unit checks.

        Args:
            self: Test owner whose session receives the receipt.
            source_id: Registered source identity for this synthetic attempt.
            error: Failure classification; None marks successful acquisition.

        Returns:
            None. Replace only that source's latest receipt, retaining other work.
        """
        source = next(s for s in self.session.sources if s.source_id == source_id)
        doc = RetrievedDocument(source_id=source_id, source_kind='url', original_location=source.url,
            attempted_at=self.session.created_at, status='failed' if error else 'retrieved', error_code=error)
        self.session.documents = [d for d in self.session.documents if d.source_id != source_id] + [doc]

    def test_retryable_failure_yields_to_other_source_and_survives_restart(self):
        """A page timeout does not monopolize scheduling or reset its retry counter.

        Args:
            self: Isolated two-source scheduler fixture.

        Returns:
            None. Assert another source executes before the delayed retry and all
            attempts survive a saved-session reconstruction.
        """
        first = self.dispatch()
        self.document('S1', 'http_timeout')
        second = self.dispatch()
        self.assertEqual(second['source_id'], 'S2')
        self.document('S2')
        self.dispatch()
        task = self.session.workflow['tasks'][first['task_id']]
        self.assertEqual(task['attempts'], 1)
        self.assertEqual(task['status'], 'pending')
        task['ready_at'] = 0
        retry = self.dispatch()
        self.assertEqual((retry['source_id'], retry['attempts']), ('S1', 2))

    def test_persistent_fetch_failure_is_bounded_without_global_budget_reset(self):
        """Persistent temporary failures stop after three attempts and remain visible.

        Args:
            self: Isolated scheduler with an independent successfully retrieved page.

        Returns:
            None. Assert exactly three attempts, retained failure and explicit stop.
        """
        self.document('S2')
        for expected in (1, 2, 3):
            for task in self.session.workflow.get('tasks', {}).values():
                task['ready_at'] = 0
            task = self.dispatch()
            self.assertEqual(task['attempts'], expected)
            self.document('S1', 'http_504')
            settle_work(self.session)
        self.assertIsNone(self.dispatch())
        self.assertEqual(self.session.workflow['stop_reason'], 'blocked')
        self.assertEqual(self.session.workflow['tasks'][task['task_id']]['attempts'], 3)

    def test_permanent_fetch_failures_are_not_retried(self):
        """TLS, robots, access rejection and ambiguous errors cannot be called transient.

        Args:
            self: Scheduler fixture reused with independent typed sessions.

        Returns:
            None. Assert each terminal classification leaves only the other page runnable.
        """
        for error in ('tls_failed', 'robots_unavailable', 'robots_denied', 'http_status',
                      'rate_limited', 'blocked_address', 'http_failed', 'file_io'):
            with self.subTest(error=error):
                self.session.workflow = {}
                self.session.documents = []
                self.document('S1', error)
                task = self.dispatch()
                self.assertEqual(task['source_id'], 'S2')

    def test_model_budget_does_not_prevent_already_authorized_acquisition(self):
        """A spent model allowance cannot discard a registered source fetch.

        Args:
            self: Scheduler fixture with the full model allowance charged.

        Returns:
            None. Assert acquisition runs without charging another model request.
        """
        self.session.usage.requests_attempted = self.session.request.budget.max_requests
        task = self.dispatch()
        self.assertEqual(task['action'], 'retrieve_sources')
        self.assertEqual(self.session.usage.requests_attempted, self.session.request.budget.max_requests)

    def test_fetch_limits_retain_blocked_tasks_and_explain_termination(self):
        """Each shared acquisition ceiling independently blocks requests before dispatch.

        Args:
            self: Two-source fixture with exact-limit counters, never a network call.

        Returns:
            None. Assert unfinished sources and the binding limit survive serialization.
        """
        limits = self.session.request.fetch_limits
        for usage, value in [('http_requests', limits.max_http_requests),
                             ('bytes_read', limits.max_total_bytes), ('elapsed_seconds', limits.max_run_seconds)]:
            with self.subTest(usage=usage):
                self.session.workflow = {}
                self.session.retrieval_usage = {usage: value}
                self.assertIsNone(self.dispatch())
                self.assertEqual(self.session.workflow['stop_reason'], 'budget_exhausted')
                self.assertEqual(self.session.workflow['queue_counts']['blocked'], 2)

    def test_cancelled_run_never_dispatches_pending_tasks(self):
        """Cancellation is an explicit stop, even when independent work remains.

        Args:
            self: Planned run cancelled before the scheduler's next turn.

        Returns:
            None. Assert no active reservation or lost pending source.
        """
        self.session.status = 'cancelled'
        self.assertIsNone(self.dispatch())
        self.assertEqual(self.session.workflow['stop_reason'], 'cancelled')
        self.assertEqual(self.session.workflow['queue_counts']['pending'], 2)

    def test_uncertain_task_is_not_replayed_but_other_work_survives(self):
        """An unknown prior outcome must not become an automatic duplicate attempt.

        Args:
            self: Run with one manually reconstructed crash reservation.

        Returns:
            None. Assert the reserved task stays uncertain and another task runs.
        """
        task = self.dispatch()
        self.session.workflow['tasks'][task['task_id']]['status'] = 'uncertain'
        self.session.workflow['active'] = None
        next_task = self.dispatch()
        self.assertEqual(next_task['source_id'], 'S2')
        self.assertEqual(self.session.workflow['tasks'][task['task_id']]['attempts'], 1)

    def test_selection_accepts_neighbors_rejects_unknown_goals_and_preserves_overflow(self):
        """Every typed proposal has an outcome without weakening ID/goal/domain checks.

        Args:
            self: Run with registered sources and a synthetic offered inventory.

        Returns:
            None. Assert valid order, invalid-item reasons, URL deduplication and
            deferred capacity independently of the malformed-response retry path.
        """
        offered = {f'L{i}': {'url': f'https://example.org/new-{i}'} for i in range(1, 7)}
        offered['L4']['url'] = offered['L1']['url']
        offered['L6']['url'] = 'https://denied.example/page'
        self.session.request.url_policy.blocked_domains.append('denied.example')
        choices = [FollowupChoice(link_id=lid, goal_ids=goals, reason='Inspect observed lead.')
            for lid, goals in [('L1', ['G1']), ('INVENTED', ['G1']), ('L1', ['G1']),
                ('L2', ['G9']), ('L3', ['G1']), ('L4', ['G1']), ('L5', ['G1']), ('L6', ['G1'])]]
        accepted, outcomes = accept_choices(self.session, choices, offered, 'link_id', 2)
        self.assertEqual([c.link_id for c in accepted], ['L1', 'L3'])
        self.assertEqual([i['reason_code'] for i in outcomes], [None, 'unknown_or_unoffered_id',
            'duplicate_id', 'unknown_goal', None, 'duplicate_url', 'capacity', 'blacklisted_domain'])
        self.assertEqual(outcomes[-2]['status'], 'deferred')

    def test_malformed_batch_retry_is_bounded_but_does_not_close_unseen_inventory(self):
        """Two unusable replies exhaust only their own offered IDs.

        Args:
            self: Unit fixture checking retry eligibility without calling a model.

        Returns:
            None. Assert a restart-equivalent receipt list preserves the unseen link.
        """
        rows = [{'link_id': 'L1'}, {'link_id': 'L2'}]
        receipt = {'offered': ['L1'], 'error': 'invalid_output_schema'}
        self.assertEqual(pending_offered_ids(rows, [receipt], 'offered', 'deferred', 'error'), ['L1', 'L2'])
        self.assertEqual(pending_offered_ids(rows, [receipt, receipt], 'offered', 'deferred', 'error'), ['L2'])

    def test_empty_valid_selection_does_not_retry_and_overflow_remains_pending(self):
        """A legitimate decline is complete work; valid capacity overflow is not.

        Args:
            self: Unit fixture with a completed two-item offered inventory.

        Returns:
            None. Assert only explicitly deferred work remains eligible.
        """
        rows = [{'result_id': 'R1'}, {'result_id': 'R2'}]
        receipt = {'offered': ['R1', 'R2'], 'deferred': ['R2'], 'error': None}
        self.assertEqual(pending_offered_ids(rows, [receipt], 'offered', 'deferred', 'error'), ['R2'])

    def test_recorded_three_item_selection_preserves_the_two_offered_links(self):
        """Regress the IDs and destinations in run 03db8e5's final failed selection.

        Args:
            self: Unit fixture with the original offered ID/URL subset copied from
                discovery.json; this test neither fetches URLs nor edits that run.

        Returns:
            None. Assert S6_L56 and S1_L70 survive the intervening invented S12_L1.
            The CLI fault-combination test separately proves continued acquisition.
        """
        offered = {
            'S6_L56': {'url': 'https://www.canada.ca/en/communications-security/news/2024/07/cse-and-nserc-to-fund-research-on-robust-secure-and-safe-artificial-intelligence.html'},
            'S1_L70': {'url': 'https://arxiv.org/abs/2403.01218'},
        }
        choices = [FollowupChoice(link_id=identifier, goal_ids=goals, reason='Recorded selection replay.')
            for identifier, goals in [('S6_L56', ['G2']), ('S12_L1', ['G1', 'G2']), ('S1_L70', ['G1'])]]
        accepted, outcomes = accept_choices(self.session, choices, offered, 'link_id', 3)
        self.assertEqual([c.link_id for c in accepted], ['S6_L56', 'S1_L70'])
        self.assertEqual([i['status'] for i in outcomes], ['accepted', 'rejected', 'accepted'])

    def test_local_work_exemption_cannot_hide_simultaneous_byte_exhaustion(self):
        """Ignoring an HTTP-only ceiling for files must not ignore a second binding limit.

        Args:
            self: Unit fixture inspecting scheduling before any source is read.

        Returns:
            None. Assert HTTP exhaustion permits local work only while byte/time
            allowances remain; simultaneous byte exhaustion blocks every source.
        """
        self.session.sources.append(FileSource(source_id='S3', path=str(self.root / 'local.txt')))
        limits = self.session.request.fetch_limits
        self.session.retrieval_usage = {'http_requests': limits.max_http_requests}
        local = next(t for t in work_inventory(self.session) if t.get('source_id') == 'S3')
        self.assertIsNone(local['blocked'])
        self.session.retrieval_usage['bytes_read'] = limits.max_total_bytes
        self.assertTrue(all(t['blocked'] == 'total_size_limit' for t in work_inventory(self.session)))
