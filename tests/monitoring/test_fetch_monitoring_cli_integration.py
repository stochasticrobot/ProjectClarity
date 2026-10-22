"""Check monitoring along the actual research CLI, graph, parser, and file-save path.

Synthetic provider/source responses keep this offline. Assertions inspect the
real event stream and snapshots, including plans that stop before retrieval and
operator-specified limits. This complements the cross-process stop tests without
turning every ordinary case into a slow worker-process test.
"""

import json
import unittest
from unittest.mock import patch

from projectclarity.run_monitor import MonitoringError, RunMonitor
from projectclarity.run_status_commands import request_cancel
from tests.retrieval import test_retrieval_cli_integration as retrieval_tests
from tests.retrieval.retrieval_test_fixtures import public_dns


class FetchMonitoringCLIIntegrationTests(unittest.TestCase):
    """Only remote dependencies are simulated; the monitoring implementation stays real."""

    def setUp(self):
        """Reuse the offline retrieval harness while keeping real monitoring in the execution path."""
        self.retrieval = retrieval_tests.RetrievalCLIIntegrationTests()
        self.retrieval.setUp()
        self.addCleanup(self.retrieval.doCleanups)
        self.cli = self.retrieval.cli

    def run_cli(self, *options):
        """Use the production module entry with source DNS and HTTP substituted."""
        return self.cli.run_cli(lambda request: self.cli.response(), *options, plan_only=False, source_dns=public_dns)

    def test_success_exposes_real_phases_counters_and_saved_document_location(self):
        """Every 'retrieved' event corresponds to files actually saved through the CLI."""
        code, _, progress = self.run_cli("--progress-json")
        self.assertEqual(code, 0, progress)
        directory, session = self.cli.saved_session()
        events = [json.loads(line) for line in progress.splitlines()]
        self.assertEqual(events, [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()])
        names = [event["event"] for event in events]
        for name in ("run_started", "planning_started", "robots_check", "request_started", "source_parsing", "source_saving", "source_finished", "run_finished"):
            self.assertIn(name, names)
        self.assertLess(names.index("source_parsing"), names.index("source_finished"))
        snapshot = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(snapshot["metrics"]["http_requests"], session["retrieval_usage"]["http_requests"])
        self.assertEqual(snapshot["metrics"]["bytes_read"], session["retrieval_usage"]["bytes_read"])
        self.assertEqual(snapshot["counts"]["retrieved"], 1)
        source = snapshot["sources"][0]
        self.assertGreater(source["elapsed_seconds"], 0)
        self.assertTrue((directory / source["text_path"]).is_file())

    def test_whole_run_limit_stops_later_sources_and_reports_why(self):
        """A zero HTTP budget makes no network request and leaves other inputs unattempted."""
        document = self.cli.directory / "later.txt"
        document.write_text("This source must not be read after the run budget stops.")
        self.cli.plan["strategy"][0]["source_ids"] = ["S1", "S2"]
        code, _, _ = self.run_cli("--max-http-requests", "0", "--document", str(document))
        self.assertEqual(code, 1)
        directory, session = self.cli.saved_session()
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(status["limits"]["max_http_requests"], 0)
        self.assertEqual(session["request"]["fetch_limits"]["max_http_requests"], 0)
        self.assertEqual(status["sources"][0]["error_code"], "request_limit")
        self.assertEqual(status["sources"][1]["status"], "not_started")
        self.assertEqual(self.retrieval.http.requests, [])
        self.assertFalse((directory / "documents/S2").exists())

    def test_clarification_has_a_waiting_status_without_claiming_any_fetch(self):
        """A deliberate review pause is neither completion nor an interrupted worker."""
        self.cli.plan["clarifications"] = ["Which scope is intended?"]
        code, _, _ = self.run_cli()
        self.assertEqual(code, 2)
        directory, _ = self.cli.saved_session()
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(status["status"], "awaiting_review")
        self.assertFalse(status["finished"])
        self.assertTrue(status["waiting_for_input"])
        self.assertEqual(status["counts"]["pending"], 1)
        self.assertEqual(self.retrieval.http.requests, [])

    def test_quiet_progress_keeps_structured_monitoring_files(self):
        """Turning off terminal chatter must not disable operational evidence."""
        code, _, progress = self.run_cli("--quiet-progress")
        self.assertEqual(code, 0, progress)
        self.assertEqual(progress, "")
        directory, _ = self.cli.saved_session()
        self.assertTrue((directory / "events.jsonl").stat().st_size > 0)

    def test_cancellation_during_planning_is_observed_before_fetching(self):
        """A stop request while the model responds keeps attempt accounting but starts no fetch."""
        def response_with_stop(request):
            """Write a real cancellation request before returning the simulated planning response."""
            directory, _ = self.cli.saved_session()
            request_cancel(self.cli.output, directory.name)
            return self.cli.response()

        code, _, _ = self.cli.run_cli(response_with_stop, plan_only=False, source_dns=public_dns)
        self.assertEqual(code, 130)
        directory, session = self.cli.saved_session()
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(session["usage"]["requests_attempted"], 1)
        self.assertEqual(status["counts"]["not_started"], 1)
        self.assertEqual(self.retrieval.http.requests, [])

    def test_monitoring_failure_stops_fetching_and_never_reports_success(self):
        """A broken log/status sink cannot let the worker continue invisibly to later sources."""
        original = RunMonitor.observe

        def fail_during_parse(monitor, event, **details):
            """Fail one monitoring event and pass all others through to test fail-visible behavior."""
            if event == "source_parsing":
                raise MonitoringError("synthetic monitoring failure")
            return original(monitor, event, **details)

        with patch.object(RunMonitor, "observe", fail_during_parse):
            code, _, _ = self.run_cli()
        self.assertEqual(code, 1)
        directory, session = self.cli.saved_session()
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(session["status"], "retrieval_failed")
        self.assertEqual(status["counts"]["retrieved"], 0)
        self.assertEqual(status["sources"][0]["error_code"], "worker_failure")
        self.assertFalse((directory / "documents/S1/text.txt").exists())
