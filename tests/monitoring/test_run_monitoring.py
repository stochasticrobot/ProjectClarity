"""Check operational records and controls without a model, website, or real run.

For example, releasing the worker lock without a final event must be reported
as interruption, not a healthy running job. Temporary folders and synthetic
sessions exercise real JSON files and file locks. These checks isolate logging,
status, and control behaviour from acquisition; cross-process CLI tests cover
the connection to the worker itself.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from projectclarity.command_line import main
from projectclarity.research_planner import ResearchError
from projectclarity.research_schemas import FetchLimits
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.run_monitor import MonitoringError, RunMonitor
from projectclarity.run_status_commands import read_status, request_cancel, run_directory
from tests.research.research_test_fixtures import example_session


class RunMonitoringTests(unittest.TestCase):
    """No application secrets, source bodies, or live network calls enter these fixtures."""

    def setUp(self):
        """Create a temporary session and idle monitor, registering lock cleanup for every outcome."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = example_session(plan_only=False)
        self.directory = self.root / self.session.session_id
        self.directory.mkdir()
        self.events = []
        self.monitor = RunMonitor(self.root, sink=self.events.append)
        self.addCleanup(self.monitor.close)

    def test_events_have_ordered_ids_and_status_counts_match_observed_outcomes(self):
        """Source completion means a reported result, not elapsed time or a guessed percent."""
        self.monitor.start(self.session)
        self.monitor.observe("source_started", source_id="S1", stage="acquiring")
        self.monitor.observe("source_finished", source_id="S1", source_status="failed", error_code="robots_denied")
        self.monitor.finish("retrieval_failed")
        status = read_status(self.directory)
        self.assertEqual(status["counts"]["failed"], 1)
        self.assertEqual(status["counts"]["completed"], 1)
        self.assertEqual(status["error_counts"], {"robots_denied": 1})
        saved = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual(saved, self.events)
        self.assertEqual([event["sequence"] for event in saved], list(range(1, len(saved) + 1)))
        self.assertTrue(all(event["run_id"] == self.session.session_id for event in saved))
        self.assertEqual(saved[2]["level"], "error")

    def test_urls_are_not_copied_into_monitoring_and_extra_payload_fields_are_refused(self):
        """A signed URL's token/path and arbitrary payloads must not enter exported logs."""
        self.session.sources[0].url = "https://example.org/PRIVATE_PATH?token=QUERY_SECRET#FRAGMENT_SECRET"
        self.monitor.start(self.session)
        with self.assertRaises(ValueError):
            self.monitor.observe("bad_event", document_text="BODY_SECRET")
        recorded = (self.directory / "events.jsonl").read_text() + (self.directory / "run_status.json").read_text()
        for secret in ("PRIVATE_PATH", "QUERY_SECRET", "FRAGMENT_SECRET", "BODY_SECRET"):
            self.assertNotIn(secret, recorded)
        self.assertIn("example.org", recorded)

    def test_log_severity_distinguishes_failures_from_review_and_normal_stop(self):
        """An operations consumer can detect failed runs even when no source was attempted."""
        self.monitor.start(self.session)
        for status, expected in [("failed", "error"), ("retrieval_failed", "error"),
                                 ("retrieval_partial", "warning"), ("needs_clarification", "warning"),
                                 ("cancelled", "info"), ("sources_retrieved", "info")]:
            with self.subTest(status=status):
                self.monitor.observe("run_outcome", run_status=status)
                self.assertEqual(self.events[-1]["level"], expected)

    def test_cancellation_request_is_idempotent_and_acknowledgement_is_separate(self):
        """A command requests a stop; only a later worker check acknowledges it."""
        self.monitor.start(self.session)
        for _ in range(2):
            self.assertIn("Cancellation requested", request_cancel(self.root, self.session.session_id))
        status = read_status(self.directory)
        self.assertTrue(status["worker_running"])
        self.assertTrue(status["cancel_requested"])
        self.assertFalse(status["finished"])
        self.assertNotIn("cancellation_observed", [event["event"] for event in self.events])
        for _ in range(2):
            with self.assertRaises(RunCancelled):
                self.monitor.check_cancel()
        self.assertEqual([event["event"] for event in self.events].count("cancellation_observed"), 1)

    def test_released_lock_without_final_status_is_detected_as_interrupted(self):
        """A crashed worker's old 'running' snapshot must not be mistaken for live work."""
        self.monitor.start(self.session)
        self.assertTrue(read_status(self.directory)["worker_running"])
        self.monitor.close()
        status = read_status(self.directory)
        self.assertFalse(status["worker_running"])
        self.assertTrue(status["interrupted"])
        with self.assertRaisesRegex(ResearchError, "no longer running"):
            request_cancel(self.root, self.session.session_id)

    def test_finished_run_does_not_get_a_new_stop_request(self):
        """An already finished or clarification-stopped run is not cancellable work."""
        self.monitor.start(self.session)
        self.monitor.finish("needs_clarification")
        self.assertIn("already finished", request_cancel(self.root, self.session.session_id))
        self.assertFalse((self.directory / "cancel.request").exists())
        self.assertEqual(read_status(self.directory)["counts"]["not_started"], 1)

    def test_run_ids_cannot_escape_the_output_folder_or_follow_run_symlinks(self):
        """An operator-supplied ID cannot redirect control files into another directory."""
        for identifier in ("../other", "/tmp", "a/b", "..", ""):
            with self.subTest(identifier=identifier), self.assertRaises(ResearchError):
                run_directory(self.root, identifier)
        (self.root / "alias").symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ResearchError):
            run_directory(self.root, "alias")

    def test_legacy_and_corrupt_status_files_have_actionable_safe_errors(self):
        """Older runs lack monitoring; corrupt JSON must not be printed as an exception payload."""
        with self.assertRaisesRegex(ResearchError, "not available"):
            read_status(self.directory)
        (self.directory / "run_status.json").write_text("PRIVATE_BROKEN_JSON")
        with self.assertRaises(ResearchError) as caught:
            read_status(self.directory)
        self.assertNotIn("PRIVATE_BROKEN_JSON", str(caught.exception))

    def test_monitor_write_failure_is_explicit_not_silently_ignored(self):
        """Operators cannot trust logs if the worker silently drops failed status writes."""
        self.monitor.start(self.session)
        with patch.object(Path, "replace", side_effect=OSError("PRIVATE_DISK_ERROR")):
            with self.assertRaises(MonitoringError) as caught:
                self.monitor.observe("planning_started", stage="planning")
        self.assertNotIn("PRIVATE_DISK_ERROR", str(caught.exception))

    def test_status_json_command_does_not_call_the_research_worker(self):
        """Monitoring commands are read-only observations, not extra paid research requests."""
        self.monitor.start(self.session)
        output = io.StringIO()
        with patch("projectclarity.command_line.run_research") as run, contextlib.redirect_stdout(output):
            code = main(["status", self.session.session_id, "--output-dir", str(self.root), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["run_id"], self.session.session_id)
        run.assert_not_called()

    def test_fetch_configuration_is_bounded_and_immutable(self):
        """Command-line limits cannot bypass the existing development/parser ceilings."""
        for fields in ({"max_source_bytes": 0}, {"max_total_bytes": 30 * 1024 * 1024},
                       {"max_http_requests": 51}, {"max_run_seconds": 0},
                       {"max_run_seconds": float("nan")}, {"request_timeout_seconds": 11}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                FetchLimits(**fields)
        limits = FetchLimits(max_http_requests=0)
        with self.assertRaises(ValidationError):
            limits.max_http_requests = 1
