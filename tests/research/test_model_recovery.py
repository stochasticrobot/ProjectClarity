"""Check limited retries of temporary model-service failures without losing attempt records.

The real command-line workflow and SDK receive synthetic HTTP errors and replies;
timing is controlled where needed. Cases cover temporary failures, authentication,
malformed output, Retry-After delays, cancellation and shared request limits across
planning, extraction, review and optional capture. They verify accounting and
recovery policy, not current provider reliability or live research accuracy.
"""
import json
import ssl
import time
import unittest
from unittest.mock import patch

import httpx2

from projectclarity.providers.model_call_inspection import inspect_calls
from projectclarity.providers.model_failures import describe_model_failure
from tests.planning import test_planning_cli_integration as planning_flow
from tests.claim_review import test_review_and_discovery_integration as review_flow

TEST_KEY = planning_flow.TEST_KEY


class ModelRecoveryTests(unittest.TestCase):
    """Keep failed wire attempts separate from accepted research outputs."""

    def setUp(self):
        """Create a real terminal-entry harness with a fast deterministic retry delay.

        Args:
            self: Isolated test case owning fixtures and cleanup callbacks.
        Returns:
            None. Network access remains forbidden by the harness.
        """
        self.cli = planning_flow.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        delay = patch('projectclarity.providers.model_retry.RETRY_BASE_SECONDS', 0)
        delay.start()
        self.addCleanup(delay.stop)

    def status_reply(self, status, **headers):
        """Construct a provider error whose credential must be redacted in receipts.

        Args:
            self: Harness owner, unused by response construction.
            status: HTTP failure code returned to the real SDK.
            **headers: Optional Retry-After and safe provider request metadata.
        Returns:
            Synthetic HTTP response; full body is retained privately with the key redacted.
        """
        return httpx2.Response(status, headers={'request-id': 'req_fixture_123', **headers},
            json={'type': 'error', 'error': {'type': 'api_error', 'message': TEST_KEY}})

    def test_planner_recovers_and_preserves_attempts(self):
        """Retry a 503 once, preserving diagnostics and unknown failed-call usage.

        Args:
            self: Case using production planning and SDK parsing.
        Returns:
            None. Assert the accepted plan, two charges and readable safe diagnostics.
        """
        replies = iter([self.status_reply(503), self.cli.response()])
        code, _, err = self.cli.run_cli(lambda request: next(replies), '--model-retries', '2', '--max-requests', '3')
        self.assertEqual(code, 0, err)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved['usage']['requests_attempted'], 2)
        self.assertIsNone(saved['usage']['total_tokens'])
        self.assertEqual([a['status'] for a in saved['model_attempts']], ['failed', 'response_received'])
        self.assertEqual(saved['model_attempts'][0]['diagnostics']['http_status'], 503)
        self.assertEqual(saved['model_attempts'][0]['diagnostics']['request_id'], 'req_fixture_123')
        self.assertEqual(json.loads((directory / 'model_attempts.json').read_text())['attempts'], saved['model_attempts'])
        inspected = inspect_calls(self.cli.output, saved['session_id'], attempt_id='M1')
        self.assertIn('service_error', inspected)
        self.assertNotIn(TEST_KEY, inspected)
        failure = saved['model_attempts'][0]['diagnostics']['provider_error_path']
        receipt = json.loads((directory / failure).read_text())
        self.assertTrue(receipt['credential_redaction_applied'])
        self.assertIn('[REDACTED_API_KEY]', receipt['errors'][0]['response_body'])
        self.assertEqual((directory / failure).stat().st_mode & 0o777, 0o600)

    def test_provider_rejection_preserves_complete_message_and_body(self):
        """Retain long schema-rejection details without retrying a bad request.

        Args:
            self: Production CLI fixture using a synthetic provider HTTP response.
        Returns:
            None; asserts full private error contents, bounded call count and
            linked diagnostics. No actual provider schema acceptance is simulated.
        """
        message = 'Schema rejected: ' + 'nested alternative detail; ' * 5000 + 'END_OF_ERROR'
        body = json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error', 'message': message}})
        code, out, err = self.cli.run_cli(lambda request: httpx2.Response(400,
            headers={'request-id': 'req_rejected_schema', 'content-type': 'application/json'},
            content=body), '--design-answer', '--max-requests', '4')
        self.assertNotEqual(code, 0)
        directory, saved = self.cli.saved_session()
        self.assertEqual(saved['usage']['requests_attempted'], 1)
        diagnostic = saved['model_attempts'][0]['diagnostics']
        self.assertEqual(diagnostic['http_status'], 400)
        receipt = json.loads((directory / diagnostic['provider_error_path']).read_text())
        self.assertEqual(receipt['errors'][0]['response_body'], body)
        self.assertIn(message, receipt['errors'][0]['message'])
        self.assertFalse(receipt['credential_redaction_applied'])
        self.assertNotIn(message, out + err)

    def test_timeout_recovers(self):
        """A typed read timeout can recover without treating its message as diagnostics.

        Args:
            self: Case controlling a first-request timeout.
        Returns:
            None. Assert one retry and a saved timeout category.
        """
        def provider(request):
            """Fail the first request and return a valid plan on the second.

            Args:
                request: Actual SDK request intercepted before network transmission.
            Returns:
                A valid response, or a synthetic typed read timeout.
            """
            if len(self.cli.requests) == 1:
                raise httpx2.ReadTimeout(TEST_KEY, request=request)
            return self.cli.response()
        code, _, err = self.cli.run_cli(provider, '--model-retries', '2', '--model-timeout-seconds', '17', '--max-requests', '3')
        self.assertEqual(code, 0, err)
        _, saved = self.cli.saved_session()
        self.assertEqual(len(saved['model_attempts']), 2)
        self.assertEqual(saved['model_attempts'][0]['diagnostics']['category'], 'timeout')
        self.assertEqual(saved['model_attempts'][0]['timeout_seconds'], 17)
        self.assertEqual(self.cli.requests[0].extensions['timeout']['read'], 17)

    def test_authentication_is_not_retried(self):
        """Authentication failures terminate without burning the retry allowance.

        Args:
            self: Case returning a provider rejection.
        Returns:
            None. Assert one request despite two configured retries.
        """
        self.cli.run_cli(lambda request: self.status_reply(401), '--model-retries', '2')
        self.assertEqual(len(self.cli.requests), 1)
        _, saved = self.cli.saved_session()
        self.assertEqual(saved['model_attempts'][0]['recovery_stop'], 'non_retryable')

    def test_retry_budget_is_bounded(self):
        """Three failed attempts exhaust two retries rather than recursively retrying.

        Args:
            self: Case returning repeatable server failures.
        Returns:
            None. Assert exact attempt and request counts.
        """
        self.cli.run_cli(lambda request: self.status_reply(503), '--model-retries', '2', '--max-requests', '10')
        _, saved = self.cli.saved_session()
        self.assertEqual(len(self.cli.requests), 3)
        self.assertEqual(saved['usage']['requests_attempted'], 3)
        self.assertEqual(saved['model_attempts'][-1]['recovery_stop'], 'retry_limit')

    def test_global_budget_prevents_retry(self):
        """Retry policy never creates extra allowance beyond max_requests.

        Args:
            self: Case with a single available request.
        Returns:
            None. Assert one charge and an explicit budget stop.
        """
        self.cli.run_cli(lambda request: self.status_reply(503), '--model-retries', '2', '--max-requests', '1')
        _, saved = self.cli.saved_session()
        self.assertEqual(len(self.cli.requests), 1)
        self.assertEqual(saved['model_attempts'][0]['recovery_stop'], 'request_limit')

    def test_long_retry_after_is_deferred(self):
        """Do not shorten a provider-requested wait or block the terminal for minutes.

        Args:
            self: Case receiving a five-minute retry delay.
        Returns:
            None. Assert no second request and retain the required delay.
        """
        self.cli.run_cli(lambda request: self.status_reply(429, **{'retry-after': '300'}), '--model-retries', '2', '--max-requests', '3')
        _, saved = self.cli.saved_session()
        self.assertEqual(len(self.cli.requests), 1)
        self.assertEqual(saved['model_attempts'][0]['recovery_stop'], 'model_retry_deferred')
        self.assertEqual(saved['model_attempts'][0]['retry_wait_seconds'], 300)

    def test_invalid_answer_is_not_retried(self):
        """A completed but malformed answer is a validation failure, not transport recovery.

        Args:
            self: Case returning invalid structured output.
        Returns:
            None. Assert one received response and no accepted plan.
        """
        code, _, _ = self.cli.run_cli(lambda request: self.cli.response(text='not JSON'), '--model-retries', '2')
        _, saved = self.cli.saved_session()
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.cli.requests), 1)
        self.assertEqual(saved['model_attempts'][0]['status'], 'response_received')

    def test_extraction_and_review_recover_without_losing_claims(self):
        """Recover each downstream path while preserving source-bound evidence exactly once.

        Args:
            self: Case using actual retrieval, extraction, review and adjudication nodes.
        Returns:
            None. Assert two preserved transport failures and one supported candidate.
        """
        flow = review_flow.ReviewDiscoveryIntegrationTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        original = flow.provider
        failed = set()
        def provider(request):
            """Inject one 503 per downstream model stage, then delegate normal replies.

            Args:
                request: Actual request with the production structured-response schema.
            Returns:
                Synthetic error once per stage, otherwise the normal evidence fixture.
            """
            title = json.loads(request.content)['output_config']['format']['schema']['title']
            if title in {'ExtractionBatch', 'ClaimReviewBatch'} and title not in failed:
                failed.add(title)
                return self.status_reply(503)
            return original(request)
        flow.provider = provider
        code, _, err, _, saved = flow.run_research('--model-retries', '2', follow=False, budget=10)
        self.assertEqual(code, 0, err)
        self.assertEqual(saved['usage']['requests_attempted'], 5)
        self.assertEqual(sum(a['status'] == 'failed' for a in saved['model_attempts']), 2)
        self.assertEqual(len(saved['extraction']['candidates']), 1)
        self.assertEqual(saved['extraction']['candidates'][0]['adjudication_status'], 'supported')
        self.assertEqual(len(saved['claim_reviews']), 1)

    def test_unknown_and_tls_errors_are_not_retryable(self):
        """Unknown adapter errors and certificate failures must not trigger blind retries.

        Args:
            self: Case exercising the fixed diagnostic allowlist.
        Returns:
            None. Assert safe categories with no exception text leakage.
        """
        for error in (RuntimeError(TEST_KEY), ssl.SSLError(TEST_KEY)):
            diagnostic = describe_model_failure(error, TEST_KEY)
            self.assertFalse(diagnostic['retryable'])
            self.assertNotIn(TEST_KEY, json.dumps(diagnostic))

    def test_retry_after_minimum_is_honored(self):
        """A short provider delay is honored before a successful second request.

        Args:
            self: Case measuring actual request arrival times at the mock transport.
        Returns:
            None. Assert no early retry and no hidden SDK retries.
        """
        arrivals = []
        def provider(request):
            """Record request time and impose a short rate-limit delay once.

            Args:
                request: Actual SDK request, used only by the transport harness.
            Returns:
                A 429 with Retry-After, then a valid plan.
            """
            arrivals.append(time.monotonic())
            return self.status_reply(429, **{'retry-after': '0.2'}) if len(arrivals) == 1 else self.cli.response()
        code, _, err = self.cli.run_cli(provider, '--model-retries', '2', '--max-requests', '3')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(arrivals), 2)
        self.assertGreaterEqual(arrivals[1] - arrivals[0], 0.2)

    def test_cancel_during_backoff_sends_no_retry(self):
        """Interrupting the wait preserves the failed attempt and spends no extra call.

        Args:
            self: Case injecting a terminal interrupt at the retry wait boundary.
        Returns:
            None. Assert cancellation, one charge and an interrupted recovery receipt.
        """
        with patch('projectclarity.providers.model_retry.time.sleep', side_effect=KeyboardInterrupt):
            self.cli.run_cli(lambda request: self.status_reply(429, **{'retry-after': '1'}),
                '--model-retries', '2', '--max-requests', '3')
        _, saved = self.cli.saved_session()
        self.assertEqual(saved['status'], 'cancelled')
        self.assertEqual(len(self.cli.requests), 1)
        self.assertEqual(saved['model_attempts'][0]['recovery_stop'], 'interrupted_before_retry')
        self.assertFalse(saved['model_attempts'][0]['retry_scheduled'])

    def test_capture_retry_obeys_auxiliary_allowance(self):
        """Optional capture exhaustion does not consume independent research capacity.

        Args:
            self: Case allowing one capture attempt within a larger research budget.
        Returns:
            None. Assert failed capture is charged once and extraction still runs.
        """
        from tests.knowledge import test_knowledge_pipeline as knowledge_flow
        fixture = knowledge_flow.KnowledgePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = fixture.provider
        def provider(request):
            """Fail capture calls while answering research calls normally.

            Args:
                request: Real SDK request whose response schema selects the fixture.
            Returns:
                A service error for capture or the ordinary planning/extraction reply.
            """
            title = json.loads(request.content)['output_config']['format']['schema']['title']
            return self.status_reply(503) if title == 'KnowledgeBatch' else original(request)
        fixture.provider = provider
        _, saved, _, _ = fixture.run_capture('--model-retries', '2', '--max-requests', '10',
            '--knowledge-max-requests', '1')
        attempts = saved['model_attempts']
        capture = [a for a in attempts if a['task'] == 'accumulate_knowledge']
        self.assertEqual(len(capture), 1)
        self.assertEqual(capture[0]['recovery_stop'], 'knowledge_request_limit')
        self.assertTrue(any(a['task'] == 'extract_knowledge' and a['status'] == 'response_received' for a in attempts))
