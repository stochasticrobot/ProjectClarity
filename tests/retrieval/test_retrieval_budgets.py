"""Check retrieval limits without waiting, downloading, or starting a model.

For example, after the run's time allowance expires, neither a new HTTP request
nor another chunk may be charged as normal work. A controlled clock makes the
exact deadline reproducible. These tests isolate budget arithmetic from the web
fetcher; they do not claim that OS DNS calls have a hard wall-clock deadline.
"""

import unittest
from unittest.mock import patch

from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError, RetrievalLimits, RunCancelled


class RetrievalBudgetTests(unittest.TestCase):
    """Counters are shared across sources and must not silently reset mid-run."""

    def test_exact_deadline_blocks_new_requests_and_bytes(self):
        """The boundary is deterministic, and rejected work does not change counters."""
        budget = RetrievalBudget(RetrievalLimits(max_run_seconds=10), started=100)
        with patch("projectclarity.retrieval.retrieval_limits.time.monotonic", return_value=109.9):
            budget.check_time()
        with patch("projectclarity.retrieval.retrieval_limits.time.monotonic", return_value=110):
            for action in (budget.check_time, budget.reserve_request, lambda: budget.charge_bytes(1)):
                with self.subTest(action=action):
                    with self.assertRaises(RetrievalError) as caught:
                        action()
                    self.assertEqual(caught.exception.code, "run_time_limit")
        self.assertEqual((budget.bytes_read, budget.http_requests), (0, 0))

    def test_request_limit_allows_exactly_the_configured_number(self):
        """An exhausted budget refuses a further request before incrementing it."""
        budget = RetrievalBudget(RetrievalLimits(max_http_requests=2))
        budget.reserve_request()
        budget.reserve_request()
        with self.assertRaises(RetrievalError) as caught:
            budget.reserve_request()
        self.assertEqual(caught.exception.code, "request_limit")
        self.assertEqual(budget.http_requests, 2)

    def test_total_byte_limit_counts_chunks_across_sources(self):
        """Many small reads cannot bypass the limit; the over-limit chunk stays counted."""
        budget = RetrievalBudget(RetrievalLimits(max_total_bytes=5))
        budget.charge_bytes(3)
        budget.charge_bytes(2)
        self.assertEqual(budget.bytes_read, 5)
        with self.assertRaises(RetrievalError) as caught:
            budget.charge_bytes(1)
        self.assertEqual(caught.exception.code, "total_size_limit")
        self.assertEqual(budget.summary()["bytes_read"], 6)

    def test_stop_request_interrupts_a_long_pacing_wait_before_any_http_attempt(self):
        """Robots waits remain cancellable instead of trapping a worker in one long sleep."""
        checks = 0

        def check_control():
            """Request a stop on the second poll to verify pacing waits are interruptible."""
            nonlocal checks
            checks += 1
            if checks == 2:
                raise RunCancelled("Stop fixture")

        budget = RetrievalBudget(check_control=check_control)
        with patch("projectclarity.retrieval.retrieval_limits.time.sleep") as sleep:
            with self.assertRaises(RunCancelled):
                budget.wait(20)
        sleep.assert_called_once_with(0.25)
        self.assertEqual(budget.http_requests, 0)
