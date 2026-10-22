"""Exercise real status, watch, cancellation, and worker-exit detection across processes.

For example, the first document must remain readable after a second terminal
cancels the slow second download. The worker fixture uses the actual research
CLI and substitutes only external HTTP/DNS. Status and cancel run as unmodified
`python -m projectclarity` commands. Temporary folders isolate every run.
These tests prove local process/file control, not remote/cloud orchestration.
"""

import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from projectclarity.run_status_commands import read_status


class WorkerControlIntegrationTests(unittest.TestCase):
    """A real worker lock and separate CLI process prevent in-process-only false proofs."""

    def setUp(self):
        """Launch one owned slow-source worker in temporary storage so other processes can control it."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "runs"
        self.cwd = Path(__file__).resolve().parents[2]
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.monitoring.monitored_worker_fixture", "--output-dir", str(self.root)],
            cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_process, self.process)

    @staticmethod
    def stop_process(process):
        """Clean up only the child launched by this test, even when an assertion fails."""
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def wait_for_slow_download(self):
        """Wait for an observed stage, not a fixed sleep guessed to mean progress."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            files = list(self.root.glob("*/run_status.json"))
            if files:
                status = json.loads(files[0].read_text())
                if status["active_source_id"] == "S2" and status["stage"] == "downloading":
                    self.directory = files[0].parent
                    self.run_id = status["run_id"]
                    return
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.fail(f"Fixture worker exited early: {stdout}\n{stderr}")
            time.sleep(0.03)
        self.fail("Worker did not reach its slow second download")

    def command(self, command, *options):
        """Use the production CLI in another process, not an in-memory control helper."""
        return subprocess.run([sys.executable, "-m", "projectclarity", command, self.run_id,
                               "--output-dir", str(self.root), *options], cwd=self.cwd,
                              capture_output=True, text=True, timeout=10)

    def assert_cancelled_with_completed_document(self):
        """Cancellation is distinct from failure; unfinished sources do not become successes."""
        _, stderr = self.process.communicate(timeout=5)
        self.assertEqual(self.process.returncode, 130, stderr)
        status = read_status(self.directory)
        self.assertTrue(status["finished"])
        self.assertFalse(status["worker_running"])
        self.assertFalse(status["interrupted"])
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual([source["status"] for source in status["sources"]], ["retrieved", "cancelled", "not_started"])
        self.assertIn("DO_NOT_LOG_DOCUMENT_BODY", (self.directory / "documents/S1/text.txt").read_text())
        self.assertFalse((self.directory / "documents/S3").exists())
        session = json.loads((self.directory / "session.json").read_text())
        self.assertEqual([doc["status"] for doc in session["documents"]], ["retrieved", "cancelled"])
        events = (self.directory / "events.jsonl").read_text()
        self.assertIn("cancellation_observed", events)
        for secret in ("QUERY_SECRET", "SYNTHETIC_KEY_DO_NOT_LOG", "DO_NOT_LOG_DOCUMENT_BODY"):
            self.assertNotIn(secret, events + stderr + (self.directory / "run_status.json").read_text())

    def test_status_watch_and_cancel_work_from_separate_terminals(self):
        """Live status precedes completion; stop requests retain S1 and prevent S3 acquisition."""
        self.wait_for_slow_download()
        observed = self.command("status", "--json")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        status = json.loads(observed.stdout)
        self.assertTrue(status["worker_running"])
        self.assertEqual(status["counts"]["retrieved"], 1)
        watcher = subprocess.Popen([sys.executable, "-m", "projectclarity", "status", self.run_id,
                                   "--output-dir", str(self.root), "--watch", "--json"], cwd=self.cwd,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, watcher)
        response = self.command("cancel")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("Cancellation requested", response.stdout)
        self.assert_cancelled_with_completed_document()
        watched, error = watcher.communicate(timeout=5)
        self.assertEqual(watcher.returncode, 0, error)
        self.assertTrue(json.loads(watched.splitlines()[-1])["finished"])

    def test_sigterm_requests_graceful_cancellation(self):
        """A normal container/service stop signal preserves the same completed artifacts."""
        self.wait_for_slow_download()
        self.process.send_signal(signal.SIGTERM)
        self.assert_cancelled_with_completed_document()

    def test_force_killed_worker_is_reported_as_interrupted_not_running(self):
        """A stale snapshot cannot falsely claim health after the OS releases its owner lock."""
        self.wait_for_slow_download()
        self.process.kill()
        self.process.communicate(timeout=5)
        response = self.command("status", "--json")
        self.assertEqual(response.returncode, 0, response.stderr)
        status = json.loads(response.stdout)
        self.assertTrue(status["interrupted"])
        self.assertFalse(status["worker_running"])
        self.assertFalse(status["finished"])
        self.assertTrue((self.directory / "documents/S1/text.txt").exists())
