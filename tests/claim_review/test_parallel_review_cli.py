"""Prove review overlap at the SDK boundary through the real CLI and scheduler.

Synthetic fixed evidence and judgments isolate execution from model variability.
Barriers require concurrent dispatch; staggered responses force out-of-order
completion. These tests establish accounting and recovery, not live speedup.
"""
import json
import threading
import time
import unittest
from collections import Counter
from unittest.mock import patch

import httpx2

from tests.claim_review import test_batched_review_cli as batches
from tests.research import test_workflow_control_cli as workflow
from projectclarity.session_storage import save_session


class ParallelReviewCLITests(unittest.TestCase):
    """Keep parallel review source-bound, budgeted and restart-safe."""

    def run_fixture(self, concurrency, *, budget=40, fault=None, coverage=False):
        """Run twenty-nine fixed claims with controlled review response ordering.

        Args:
            self: Test owner registering temporary cleanup.
            concurrency: Real CLI worker limit, one or three.
            budget: Total model allowance including planning and extraction.
            fault: Optional malformed, retry, cancel or crash fault in the first batch.
            coverage: Enable real coverage scheduling and its reserved final call.
        Returns:
            Fixture, CLI result, observed intervals and provider callable for resume.
            Only HTTP responses are synthetic; reservations, SDK parsing and commits
            execute normally. First review responses synchronize before returning.
        """
        case = batches.BatchedReviewCLITests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.claim_count = 29
        case.flow.cli.urls.write_text('https://example.org/seed0\n')
        case.flow.routes[('example.org', '/seed0')] = (200,
            {'Content-Type': 'text/plain'}, b'Group 0 researches attacks.')
        barrier = threading.Barrier(min(concurrency, max(1, budget - 2)))
        lock = threading.Lock()
        intervals, calls, armed = [], Counter(), [True]

        def provider(request):
            """Synchronize real review requests, then return independently owned replies.

            Args:
                request: Actual SDK HTTP request; non-review calls use the fixture.
            Returns:
                Provider-shaped reply, transient429, malformedJSON, or a simulated
                process interruption. Records wire intervals under a lock; reads
                saved assignments before replying to prove pre-dispatch durability.
            """
            body = json.loads(request.content)
            if body['output_config']['format']['schema']['title'] == 'CoverageAssessment':
                data = json.loads(body['messages'][0]['content'])
                return case.flow.cli.response(text=json.dumps(dict(assessments=[dict(
                    criterion_id=c['criterion_id'], judgment='unknown', items=[],
                    target_count=None, benchmark_candidate_ids=[], evidence_candidate_ids=[],
                    gaps=['Synthetic incomplete evidence.'], rationale='Not independently complete.')
                    for c in data['criteria']], next_searches=[])))
            if body['output_config']['format']['schema']['title'] != 'ClaimReviewBatch':
                return case.provider(request)
            payload = json.loads(body['messages'][0]['content'])
            ids = tuple(c['candidate_id'] for c in payload['candidates'])
            with lock:
                calls[ids] += 1
                first = calls[ids] == 1
                ordinal = len(calls) - 1
                active_fault = fault if armed[0] else None
            saved = case.flow.cli.saved_session()[1]
            reservations = saved['workflow']['review_batches'].values()
            self.assertTrue(any(set(ids) == set(r['assignments']) for r in reservations))
            start = time.monotonic()
            if first and len(calls) <= barrier.parties:
                barrier.wait(timeout=10)
            if ordinal == 0 and active_fault:
                with lock:
                    armed[0] = False
                if active_fault == 'crash':
                    raise SystemExit(77)
                if active_fault == 'cancel':
                    raise KeyboardInterrupt()
                if active_fault == 'retry':
                    return httpx2.Response(429, json={'error': {'type': 'rate_limit_error',
                        'message': 'Synthetic bounded retry'}}, headers={'retry-after-ms': '1'})
                if active_fault == 'malformed':
                    return case.flow.cli.response(text='{')
            # Earlier reservations finish later, exposing order-sensitive commits.
            time.sleep(0.06 * (3 - ordinal) if ordinal < 3 else 0.01)
            response = case.provider(request)
            with lock:
                intervals.append((ids, start, time.monotonic()))
            return response

        coordinator = threading.get_ident()

        def coordinator_save(session, output_root):
            """Assert review journal writes never execute on provider workers.

            Args:
                session: Real mutable production session.
                output_root: Actual temporary run root.
            Returns:
                Original save result; asserts caller ownership before saving.
            """
            self.assertEqual(threading.get_ident(), coordinator)
            return save_session(session, output_root)

        case.flow.provider = provider
        with (patch('projectclarity.nodes.review_claims.node.save_session', coordinator_save),
              patch('projectclarity.providers.structured_model_call_execution.save_session', coordinator_save)):
            result = case.flow.run_research('--model-concurrency', str(concurrency),
                '--model-retries', '1' if fault == 'retry' else '0',
                *(['--assess-coverage'] if coverage else []), follow=False, budget=budget)
        return case, result, intervals, provider

    def test_actual_overlap_and_serial_parity(self):
        """Require overlapping wires, out-of-order commits and identical final verdicts.

        Args:
            self: Isolated CLI test owner.
        Returns:
            None; all29 claims are reviewed exactly once with equivalent serial and
            parallel results, same call count and only coordinator-owned journals.
        """
        results = []
        for concurrency in [1, 3]:
            case, result, intervals, _ = self.run_fixture(concurrency)
            code, _, err, _, saved = result
            self.assertEqual(code, 0, err[-1000:])
            case.assert_accounted(saved)
            ordered = sorted(intervals, key=lambda r: r[1])
            overlaps = sum(b[1] < a[2] for i, a in enumerate(ordered) for b in ordered[i+1:])
            self.assertEqual(overlaps > 0, concurrency > 1)
            if concurrency > 1:
                self.assertNotEqual([r[0] for r in ordered], [r[0] for r in intervals])
            self.assertEqual(len(saved['model_attempts']), 5)
            results.append({k: v['assessment'] for k, v in saved['claim_reviews'].items()})
        self.assertEqual(results[0], results[1])

    def test_malformed_peer_does_not_discard_successful_batches(self):
        """Keep independent successful outcomes when one parallel reply is invalid.

        Args:
            self: CLI fixture owner.
        Returns:
            None; twelve claims fail explicitly while seventeen retain valid reviews.
        """
        case, result, _, _ = self.run_fixture(3, fault='malformed')
        saved = result[-1]
        case.assert_accounted(saved)
        self.assertEqual(sum(r['assessment'] is not None for r in saved['claim_reviews'].values()), 17)

    def test_retry_cannot_overspend_shared_wave_budget(self):
        """Exhaust a small shared allowance while independent requests are in flight.

        Args:
            self: CLI test owner.
        Returns:
            None; five total attempts leave no hidden retry allowance, and all
            twenty-nine assigned claims retain an outcome despite the429 response.
        """
        _, result, _, _ = self.run_fixture(3, budget=5, fault='retry')
        saved = result[-1]
        self.assertEqual(saved['usage']['requests_attempted'], 5)
        self.assertEqual(len(saved['model_attempts']), 5)
        self.assertEqual(len(saved['claim_reviews']), 29)

    def test_interrupted_wave_is_not_resent_on_resume(self):
        """Crash after concurrent dispatch and preserve unknown outcomes on resume.

        Args:
            self: CLI test owner retaining the interrupted run directory.
        Returns:
            None; resume makes no new review requests for already assigned claims.
            In-flight responses may be uncommitted and are not treated as zero cost.
        """
        case, result, _, provider = self.run_fixture(3, fault='crash')
        self.assertEqual(result[0], 77)
        saved = result[-1]
        before = len(saved['model_attempts'])
        # Allow detached simulated transport calls to finish before fixture cleanup.
        time.sleep(0.3)
        controller = workflow.WorkflowControlCLITests()
        controller.cli, controller.provider = case.flow.cli, provider
        _, resumed = controller.resume(saved['session_id'])
        self.assertEqual(len(resumed['model_attempts']), before)
        self.assertEqual(len(resumed['claim_reviews']), 29)

    def test_parallel_retry_preserves_final_coverage_slot(self):
        """Verify review retries cannot spend the production final-assessment reserve.

        Args:
            self: Real CLI fixture owner with a deliberately tight allowance.
        Returns:
            None; a coverage wire request survives the bounded parallel review wave,
            and all attempts, including the transient failure, fit the shared limit.
        """
        _, result, _, _ = self.run_fixture(3, budget=6, fault='retry', coverage=True)
        saved = result[-1]
        self.assertLessEqual(saved['usage']['requests_attempted'], 6)
        self.assertTrue(any(a['task'] == 'bounded_coverage' for a in saved['model_attempts']))
        self.assertEqual(saved['model_attempts'][-1]['task'], 'bounded_coverage')

    def test_successful_retry_retains_one_assignment_and_all_attempts(self):
        """Let a transient failure retry alongside peers without double assignment.

        Args:
            self: CLI test owner with adequate shared allowance.
        Returns:
            None; three logical batches create four review wire attempts, with
            one saved retry and all twenty-nine claims reviewed successfully.
        """
        case, result, _, _ = self.run_fixture(3, fault='retry')
        saved = result[-1]
        case.assert_accounted(saved)
        attempts = [a for a in saved['model_attempts'] if a['task'] == 'review_claims']
        self.assertEqual(len(attempts), 4)
        self.assertEqual(sorted(Counter(a['call_id'] for a in attempts).values()), [1, 1, 2])
        self.assertTrue(all(r['assessment'] is not None for r in saved['claim_reviews'].values()))

    def test_user_cancellation_retains_wave_outcomes(self):
        """Distinguish explicit cancellation from recoverable process interruption.

        Args:
            self: CLI owner cancelling one of three concurrent requests.
        Returns:
            None; run remains explicitly cancelled, all assigned claims retain
            outcomes and workers perform no late journal writes.
        """
        _, result, _, _ = self.run_fixture(3, fault='cancel')
        saved = result[-1]
        self.assertEqual(saved['status'], 'cancelled')
        self.assertEqual(len(saved['claim_reviews']), 29)
        directory = result[-2]
        before = (directory / 'session.json').read_bytes()
        time.sleep(0.3)
        self.assertEqual((directory / 'session.json').read_bytes(), before)
