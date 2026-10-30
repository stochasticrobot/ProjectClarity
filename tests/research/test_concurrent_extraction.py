"""Check overlapping passage requests and single-threaded research-state updates.

The command-line workflow and provider SDK run against delayed simulated responses
that finish out of order. Tests inspect actual request overlap, saved budgets,
retries, cancellation and restart behavior, plus source-priority decisions. This
checks execution mechanics, not live throughput under provider rate limits.
"""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

from tests.research import test_document_continuation_cli as documents
from tests.research import test_document_continuation as focus_cases
from projectclarity.nodes.schedule_work.document_continuation import document_work
from projectclarity.nodes.schedule_work.source_priority import source_priority
from projectclarity.research_schemas import URLSource, ResearchRequest


class ConcurrentExtractionTests(unittest.TestCase):
    """Exercise actual overlapping extraction and a single durable state owner."""

    def setUp(self):
        """Prepare real graph transports with a multi-passage source and bounded latency.

        Args:
            self: Test owner with a disposable research run root.
        Returns:
            None. No user artifacts, actual credentials or live network are used.
        """
        self.doc = documents.DocumentContinuationCLITests()
        self.doc.setUp()
        self.addCleanup(self.doc.doCleanups)
        self.flow = self.doc.flow
        self.original = self.flow.provider
        self.lock = threading.Lock()
        self.active = self.peak = self.count = 0
        self.finished = []
        self.started = []
        self.barrier = None
        self.fail_first = False
        self.crash_first = False
        self.cancel_wave = False
        self.clock_offset = 0
        self.expire_wave = False
        self.flow.provider = self.provider

    def provider(self, request):
        """Force observable overlap and out-of-order completion at the wire boundary.

        Args:
            self: Fixture retaining synchronized observations only, not research state.
            request: Actual SDK request passed to the offline HTTP transport.
        Returns:
            Original fixture answer, a transient 503, or an intentional abrupt exit.
            Active counters always unwind, even if the barrier or provider fails.
        """
        body = json.loads(request.content)
        if body['output_config']['format']['schema']['title'] != 'ExtractionBatch':
            return self.original(request)
        payload = json.loads(body['messages'][0]['content'])
        with self.lock:
            self.count += 1
            number = self.count
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.append((payload['source_id'], payload['passage']['passage_id']))
        try:
            if self.barrier and number <= self.barrier.parties:
                self.barrier.wait(timeout=5)
            if number == 3 and self.cancel_wave:
                directory = next(p.parent for p in self.flow.cli.output.glob('*/session.json'))
                (directory / 'cancel.request').write_text('cancel')
            if number == 3 and self.expire_wave:
                self.clock_offset = 3600
            delay = (0.01 if number == 1 else 0.2) if self.crash_first else (0.06 if number == 1 else 0.01)
            time.sleep(0.2 if self.cancel_wave else delay)
            if self.crash_first and number == 1:
                raise SystemExit(77)
            if self.fail_first and number == 1:
                import httpx2
                return httpx2.Response(503, json={'type': 'error', 'error':
                    {'type': 'api_error', 'message': 'Synthetic transient error'}})
            return self.original(request)
        finally:
            with self.lock:
                self.active -= 1
                self.finished.append(number)

    def test_three_wire_calls_overlap_out_of_order_with_single_writer(self):
        """Require real overlap, central writes, exact charges and retained proposals.

        Args:
            self: Production entry fixture with three mutually waiting requests.
        Returns:
            None. A sequential implementation fails the barrier, not just a timing guess.
        """
        from projectclarity.providers import model_retry
        owner = threading.get_ident()
        writers = []
        original = model_retry.save_model_attempts
        def save(session, root):
            """Observe journal ownership without replacing persistence.

            Args:
                session: Real run being saved.
                root: Real temporary output root.
            Returns:
                Original writer result; records this caller's thread identity.
            """
            writers.append(threading.get_ident())
            return original(session, root)
        self.barrier = threading.Barrier(3)
        with patch.object(model_retry, 'save_model_attempts', side_effect=save):
            _, directory, saved, _ = self.flow.run_workflow('--model-concurrency', '3', '--max-requests', '18')
        self.assertEqual(self.peak, 3)
        self.assertNotEqual(self.finished[0], 1)
        self.assertEqual(set(writers), {owner})
        self.assertEqual(saved['request']['model_concurrency'], 3)
        self.assertEqual(saved['usage']['requests_attempted'], len(saved['model_attempts']))
        self.assertLessEqual(len(saved['model_attempts']), 18)
        self.assertEqual(len({a['attempt_id'] for a in saved['model_attempts']}), len(saved['model_attempts']))
        self.assertTrue(saved['extraction']['candidates'])
        self.assertFalse(any(a['status'] == 'started' for a in saved['model_attempts']))
        self.assertEqual(json.loads((directory / 'model_attempts.json').read_text())['attempts'], saved['model_attempts'])
        ids = [c['candidate_id'] for c in saved['extraction']['candidates']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_single_request_mode_and_small_shared_budget(self):
        """Keep the sequential escape hatch and never dispatch beyond the shared cap.

        Args:
            self: Offline source/model fixture.
        Returns:
            None. Verifies actual wire concurrency and saved attempted-call denominator.
        """
        _, _, saved, _ = self.flow.run_workflow('--model-concurrency', '1', '--max-requests', '6')
        self.assertEqual(self.peak, 1)
        self.assertEqual(saved['usage']['requests_attempted'], 6)
        self.assertEqual(len(saved['model_attempts']), 6)

    def test_retry_with_peers_uses_same_budget_and_keeps_failed_receipt(self):
        """Overlap one transient retry with independent work without duplicate outputs.

        Args:
            self: Three-slot fixture failing its first extraction wire attempt.
        Returns:
            None. Retains both attempts of one passage within the unchanged cap.
        """
        self.fail_first = True
        self.barrier = threading.Barrier(3)
        with patch('projectclarity.providers.model_retry.RETRY_BASE_SECONDS', 0):
            _, _, saved, _ = self.flow.run_workflow('--model-concurrency', '3', '--model-retries', '1', '--max-requests', '12')
        failures = [a for a in saved['model_attempts'] if a['status'] == 'failed']
        self.assertEqual(len(failures), 1)
        retries = [a for a in saved['model_attempts'] if a['call_id'] == failures[0]['call_id']]
        self.assertEqual([a['status'] for a in retries], ['failed', 'response_received'])
        self.assertLessEqual(len(saved['model_attempts']), 12)
        self.assertEqual(saved['usage']['requests_attempted'], len(saved['model_attempts']))

    def test_interruption_resume_never_replays_sent_parallel_passages(self):
        """Retain peer outcomes and charges when one concurrent request interrupts the run.

        Args:
            self: Real journal/resume fixture with a transport-level abrupt exit.
        Returns:
            None. Resuming does not repeat charged passage IDs or planning.
        """
        self.barrier = threading.Barrier(3)
        self.crash_first = True
        self.flow.run_workflow('--model-concurrency', '3', '--max-requests', '12')
        _, before = self.flow.cli.saved_session()
        charged = [(a['task'], a['call_id']) for a in before['model_attempts']]
        self.assertEqual(sum(a['status'] == 'uncertain' for a in before['model_attempts']), 3)
        self.crash_first = False
        self.barrier = None
        _, saved = self.flow.resume(before['session_id'])
        after = [(a['task'], a['call_id']) for a in saved['model_attempts']]
        for task in charged:
            self.assertEqual(after.count(task), 1)
        self.assertLessEqual(len(after), 12)
        self.assertEqual(sum(t == 'plan_research' for t, _ in after), 1)
        self.assertFalse(any(a['status'] == 'started' for a in saved['model_attempts']))

    def test_concurrent_deadline_stops_new_dispatch_without_exhausting_calls(self):
        """Allow a wave to finish but forbid new calls after its shared clock expires.

        Args:
            self: Three-slot fixture advancing only the persisted wall-clock budget.
        Returns:
            None. Reports the time stop with unused call capacity and queued work.
        """
        self.barrier = threading.Barrier(3)
        self.expire_wave = True
        real_time, real_monotonic = time.time, time.monotonic
        with patch('time.time', side_effect=lambda: real_time() + self.clock_offset), patch(
                'time.monotonic', side_effect=lambda: real_monotonic() + self.clock_offset):
            _, _, saved, _ = self.flow.run_workflow('--model-concurrency', '3', '--max-requests', '18')
        self.assertEqual(self.count, 3)
        self.assertLess(saved['usage']['requests_attempted'], 18)
        self.assertIn('run_time_limit', saved['workflow']['stop_details'])
        self.assertFalse(any(a['status'] == 'started' for a in saved['model_attempts']))

    def test_cancellation_leaves_no_worker_owned_writes(self):
        """Cancel in-flight requests and verify late network returns cannot rewrite the run.

        Args:
            self: Three synthetic requests that finish after cancellation is observed.
        Returns:
            None. Keeps charges and terminal receipts; late workers have no writer access.
        """
        self.barrier = threading.Barrier(3)
        self.cancel_wave = True
        _, directory, saved, _ = self.flow.run_workflow('--model-concurrency', '3', '--max-requests', '18')
        self.assertEqual(saved['status'], 'cancelled')
        self.assertEqual(self.count, 3)
        before = (directory / 'session.json').read_bytes()
        time.sleep(0.3)
        self.assertEqual((directory / 'session.json').read_bytes(), before)
        self.assertFalse(any(a['status'] == 'started' for a in saved['model_attempts']))
        self.assertEqual(saved['usage']['requests_attempted'], len(saved['model_attempts']))

    def test_official_registered_source_is_fetched_and_read_first(self):
        """Verify configurable institution preference through source acquisition and extraction.

        Args:
            self: Real graph with the nonofficial source deliberately listed first.
        Returns:
            None. The second seed wins by configured host, not title or source order.
        """
        self.flow.cli.urls.write_text('https://example.org/research\nhttps://registry.example.edu/research\n')
        for path in ('/robots.txt', '/research'):
            self.flow.routes[('registry.example.edu', path)] = self.flow.routes[('example.org', path)]
        _, _, saved, _ = self.flow.run_workflow('--model-concurrency', '3',
            '--official-domain', 'registry.example.edu', '--max-requests', '15')
        self.assertEqual(saved['documents'][0]['source_id'], 'S2')
        self.assertEqual(self.started[0][0], 'S2')
        self.assertTrue(any(a['status'] == 'completed' and a['source_id'] == 'S2'
            for a in saved['extraction']['attempts']))


class OfficialPriorityTests(unittest.TestCase):
    """Keep authority hints domain-bounded and independent of evidence acceptance."""

    def test_official_signal_displaces_secondary_focus_without_verifying_it(self):
        """Prefer an official same-goal source while preserving the original pending claim.

        Args:
            self: Assertion owner using minimal source-focus receipts.
        Returns:
            None. Verifies an explanatory switch reason and unchanged candidate status.
        """
        fixture = focus_cases.DocumentContinuationTests()
        fixture.setUp()
        s = fixture.session
        s.sources = [URLSource(source_id='S1', url='https://en.wikipedia.org/wiki/Test'),
                     URLSource(source_id='S2', url='https://www.ourcommons.ca/Members/en/search')]
        s.request.official_domains = []
        document_work(s, fixture.available)
        other = {**copy.deepcopy(fixture.candidate), 'source_id': 'S2', 'text_sha256': 'two',
            'candidate_id': 'S2_P0_C1'}
        s.extraction['candidates'].append(other)
        document_work(s, fixture.available)
        self.assertEqual(s.workflow['document_focus']['source_id'], 'S2')
        self.assertEqual(s.workflow['document_focus_history'][0]['release_reason'], 'official_source_priority')
        self.assertEqual(other['adjudication_status'], 'pending')
        for _ in range(3):
            document_work(s, fixture.available)
            self.assertEqual(s.workflow['document_focus']['source_id'], 'S2')

    def test_lookalikes_and_redirects_do_not_inherit_official_priority(self):
        """Match domain boundaries and actual acquired destinations, not official-looking text.

        Args:
            self: Assertion owner with a synthetic source and final URL.
        Returns:
            None. Redirect-away and deceptive hosts are ordinary scheduling sources.
        """
        fixture = focus_cases.DocumentContinuationTests()
        fixture.setUp()
        s = fixture.session
        s.request.official_domains = []
        s.sources = [URLSource(source_id='S1', url='https://ourcommons.ca.evil.example/official')]
        self.assertEqual(source_priority(s, 'S1'), 1)
        s.sources[0].url = 'https://ourcommons.ca/Members'
        self.assertEqual(source_priority(s, 'S1'), 0)
        s.documents[0].final_url = 'https://example.org/official'
        self.assertEqual(source_priority(s, 'S1'), 1)
        with self.assertRaises(ValueError):
            ResearchRequest(question='test', model_concurrency=0)
        with self.assertRaises(ValueError):
            ResearchRequest(question='test', official_domains=['https://example.org'])
