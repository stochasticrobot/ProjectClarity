"""Prove that a real LangGraph interrupt survives across command-line processes.

The initial research command uses the existing synthetic Anthropic HTTP harness.
Review commands run in separate OS processes, and document parsing/storage stay
real. All sources are temporary local documents: no real key, provider credits,
web server, or network is needed. Other tests call the same public service a
future web adapter would use, without importing terminal UI logic.

These checks establish durable local review/continuation, not browser delivery,
multi-user authorization, distributed execution, or arbitrary crash recovery.
These runs do not enable extraction, so continuation needs no model call. Opted-in
extraction after review is separately checked in tests/extraction/.
"""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from projectclarity.plan_review_contracts import PlanReviewDecision, ReviewAnswer
from projectclarity.plan_review_service import get_plan_review, submit_plan_review
from projectclarity.research_planner import ResearchError
from projectclarity.run_monitor import RunMonitor
from projectclarity.run_status_commands import read_status
from tests.planning import test_planning_cli_integration as planning_tests


class PlanReviewIntegrationTests(unittest.TestCase):
    """Use real persisted interrupts rather than pretending a saved plan can resume."""

    def setUp(self):
        """Prepare a local document and two blocking questions to exercise durable review pauses."""
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.document = self.cli.directory / "evidence.txt"
        self.document.write_text("Local evidence acquired after human review.\n", encoding="utf-8")
        self.cli.plan["clarifications"] = ["PRIVATE_REVIEW_QUESTION: which years?", "Include organizations?"]

    def pause(self, *options):
        """Run the real research entry point; require zero acquisition before review."""
        code, _, _ = self.cli.run_cli(lambda request: self.cli.response(),
            "--document", str(self.document), *options, include_urls=False, plan_only=False)
        self.assertEqual(code, 2)
        self.directory, self.session = self.cli.saved_session()
        self.assertEqual(self.session["status"], "awaiting_review")
        self.assertFalse((self.directory / "documents.json").exists())
        self.review = get_plan_review(self.cli.output, self.directory.name)

    def command(self, name, *options):
        """A fresh interpreter must load the checkpoint, not reuse in-memory graph state."""
        env = dict(os.environ, ANTHROPIC_API_KEY="", LANGSMITH_TRACING="false")
        return subprocess.run([sys.executable, "-m", "tests.review.review_cli_process", name, self.directory.name,
            "--output-dir", str(self.cli.output), *options], capture_output=True, text=True, env=env, timeout=20)

    def decision(self, action="accept_assumptions", **fields):
        """Construct the same response object that an HTTP adapter would validate."""
        return PlanReviewDecision(run_id=self.directory.name, review_id=self.review.review_id, action=action, **fields)

    def submit(self, decision):
        """Deny model setup/credentials/network while exercising the real resume path."""
        with patch("projectclarity.agentic_workflow.create_planner", side_effect=AssertionError("Review reran planning")), \
             patch("projectclarity.research_planner.read_api_key", side_effect=AssertionError("Review read a key")), \
             patch("socket.socket.connect", side_effect=AssertionError("Review used the network")):
            return submit_plan_review(self.cli.output, decision)

    def test_separate_cli_processes_inspect_and_resume_the_same_run(self):
        """Durability means the original process can exit before approval and acquisition."""
        self.pause()
        inspected = self.command("review", "--json")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout), self.review.model_dump(mode="json"))
        watched = self.command("status", "--watch", "--json")
        self.assertEqual(watched.returncode, 0, watched.stderr)
        status = json.loads(watched.stdout)
        self.assertTrue(status["waiting_for_input"])
        self.assertFalse(status["worker_running"])
        self.assertFalse(status["interrupted"])
        self.assertFalse(status["finished"])
        continued = self.command("review", "--review-id", self.review.review_id, "--accept-assumptions", "--quiet-progress")
        self.assertEqual(continued.returncode, 0, continued.stderr)
        saved = json.loads((self.directory / "session.json").read_text())
        self.assertEqual(saved["status"], "sources_retrieved")
        self.assertEqual(saved["session_id"], self.session["session_id"])
        self.assertEqual(saved["usage"], self.session["usage"])
        self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research", "review_plan", "retrieve_sources"])
        self.assertEqual((self.directory / "documents/S1/text.txt").read_text(), self.document.read_text())
        self.assertEqual(len(self.cli.requests), 1)
        final = read_status(self.directory)
        self.assertTrue(final["finished"])
        self.assertFalse(final["waiting_for_input"])

    def test_cli_answers_are_recorded_as_human_guidance_without_another_call(self):
        """Answers survive in the decision and readable plan, not only terminal output."""
        self.pause()
        answers = self.cli.directory / "answers.json"
        answers.write_text(json.dumps({"Q1": "PRIVATE_ANSWER: 2024–2026", "Q2": "Yes, organizations and people."}))
        result = self.command("review", "--review-id", self.review.review_id, "--answers", str(answers))
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads((self.directory / "session.json").read_text())
        self.assertEqual(saved["review_decision"]["action"], "answer")
        self.assertIn("PRIVATE_ANSWER", saved["review_decision"]["answers"][0]["answer"])
        self.assertEqual(saved["plan"], self.session["plan"])  # No invented LLM revision.
        self.assertIn("human guidance", (self.directory / "plan.md").read_text())
        self.assertNotIn("PRIVATE_ANSWER", (self.directory / "events.jsonl").read_text())

    def test_service_can_accept_a_manually_revised_plan_without_cli_code(self):
        """The future web adapter has a callable transport-neutral revision interface."""
        self.pause()
        revised = self.review.plan.model_copy(deep=True)
        revised.objective = "The reviewer's narrower objective"
        revised.clarifications = []
        decision = self.decision("answer", revised_plan=revised, answers=[
            ReviewAnswer(question_id="Q1", answer="2024–2026"), ReviewAnswer(question_id="Q2", answer="Yes")])
        session = self.submit(decision)
        self.assertEqual(session.status, "sources_retrieved")
        self.assertEqual(session.plan.objective, revised.objective)
        original = json.loads((self.directory / "review_request.json").read_text())
        self.assertEqual(original["plan"], self.cli.plan)
        self.assertEqual(json.loads((self.directory / "plan.json").read_text()), revised.model_dump())

    def test_rejection_finishes_without_fetching_or_rerunning_the_planner(self):
        """Reject is a real graph branch, not a label attached to successful retrieval."""
        self.pause()
        result = self.submit(self.decision("reject", note="Scope is unsuitable."))
        self.assertEqual(result.status, "rejected")
        self.assertNotIn("retrieve_sources", result.visited_nodes)
        self.assertFalse((self.directory / "documents.json").exists())
        self.assertTrue(read_status(self.directory)["finished"])

    def test_stale_and_incomplete_answers_leave_the_pause_unchanged(self):
        """Invalid responses do not consume the interrupt, create events, or start work."""
        self.pause()
        before = (self.directory / "events.jsonl").read_bytes()
        stale = self.decision()
        stale.review_id = "another-review"
        invalid = [stale, self.decision("answer", answers=[]), self.decision("answer", answers=[
            ReviewAnswer(question_id="Q1", answer="2024"), ReviewAnswer(question_id="Q999", answer="Yes")])]
        for decision in invalid:
            with self.subTest(decision=decision.model_dump()):
                with self.assertRaises(ResearchError):
                    self.submit(decision)
                self.assertEqual(get_plan_review(self.cli.output, self.directory.name), self.review)
                self.assertEqual((self.directory / "events.jsonl").read_bytes(), before)
                self.assertFalse((self.directory / "documents.json").exists())

    def test_duplicate_submission_cannot_fetch_documents_twice(self):
        """A repeated click after success is refused without changing receipts/events."""
        self.pause()
        decision = self.decision()
        self.submit(decision)
        before = {name: (self.directory / name).read_bytes() for name in ("events.jsonl", "documents.json", "session.json")}
        with self.assertRaisesRegex(ResearchError, "No pending"):
            self.submit(decision)
        for name, content in before.items():
            self.assertEqual((self.directory / name).read_bytes(), content)

    def test_concurrent_submission_cannot_take_the_active_worker_lock(self):
        """A second process must fail before consuming the current review decision."""
        self.pause()
        monitor = RunMonitor(self.cli.output)
        try:
            monitor.attach(self.directory)
            result = self.command("review", "--review-id", self.review.review_id, "--accept-assumptions")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("active worker", result.stderr)
        finally:
            monitor.close()
        self.assertEqual(get_plan_review(self.cli.output, self.directory.name), self.review)
        self.assertFalse((self.directory / "documents.json").exists())

    def test_operational_events_identify_reviews_without_containing_their_text(self):
        """A UI can react to metadata and separately request authorized private details."""
        self.pause()
        self.submit(self.decision())
        events = [json.loads(line) for line in (self.directory / "events.jsonl").read_text().splitlines()]
        self.assertEqual([item["sequence"] for item in events], list(range(1, len(events) + 1)))
        requested = [item for item in events if item["event"] == "review_required"]
        resolved = [item for item in events if item["event"] == "review_resolved"]
        self.assertEqual(len(requested), 1)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(requested[0]["details"]["review_id"], resolved[0]["details"]["review_id"])
        self.assertEqual(requested[0]["details"]["question_count"], 2)
        self.assertNotIn("PRIVATE_REVIEW_QUESTION", json.dumps(events))
        self.assertEqual(sum(item["event"] == "planning_started" for item in events), 1)

    def test_approval_keeps_original_fetch_limits(self):
        """A human approval is not permission to bypass the run's byte allowance."""
        self.pause("--max-source-bytes", "1")
        result = self.submit(self.decision())
        self.assertEqual(result.status, "retrieval_failed")
        self.assertEqual(result.request.fetch_limits.max_source_bytes, 1)
        self.assertEqual(result.documents[0].status, "failed")
        self.assertFalse((self.directory / "documents/S1/text.txt").exists())

    def test_changed_local_source_is_rechecked_after_review(self):
        """A previously registered file replaced by a symlink cannot inherit approval."""
        self.pause()
        replacement = self.cli.directory / "private.txt"
        replacement.write_text("Must not be acquired")
        self.document.unlink()  # Only this test's temporary fixture is replaced.
        self.document.symlink_to(replacement)
        result = self.submit(self.decision())
        self.assertEqual(result.status, "retrieval_failed")
        self.assertFalse((self.directory / "documents/S1/raw.bin").exists())

    def test_revised_plan_cannot_introduce_an_unregistered_source(self):
        """A manually edited plan still passes source-ID validation before continuation."""
        self.pause()
        revised = self.review.plan.model_copy(deep=True)
        revised.clarifications = []
        revised.strategy[0].source_ids = ["S999"]
        with self.assertRaisesRegex(ResearchError, "originally supplied"):
            self.submit(self.decision("answer", revised_plan=revised, answers=[
                ReviewAnswer(question_id="Q1", answer="2024"), ReviewAnswer(question_id="Q2", answer="Yes")]))
        self.assertEqual(get_plan_review(self.cli.output, self.directory.name), self.review)

    def test_edited_display_file_cannot_change_the_checkpoint_review(self):
        """The convenient JSON artifact is a projection, not authority to change a plan."""
        self.pause()
        path = self.directory / "review_request.json"
        edited = json.loads(path.read_text())
        edited["review_id"] = "forged-review"
        path.write_text(json.dumps(edited))
        self.assertEqual(get_plan_review(self.cli.output, self.directory.name), self.review)

    def test_crashed_execution_is_not_mistaken_for_a_safe_review_pause(self):
        """The review command is not an unsafe general-purpose replay of crashed tasks."""
        self.pause()
        path = self.directory / "run_status.json"
        status = json.loads(path.read_text())
        status["waiting_for_input"] = False  # Simulate worker death after resume starts.
        path.write_text(json.dumps(status))
        self.assertTrue(read_status(self.directory)["interrupted"])
        with self.assertRaisesRegex(ResearchError, "No pending"):
            self.submit(self.decision())
        self.assertFalse((self.directory / "documents.json").exists())

    def test_cli_requires_the_inspected_review_id_before_approving(self):
        """A convenience flag must not silently approve whatever review is current."""
        self.pause()
        result = self.command("review", "--accept-assumptions")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--review-id", result.stderr)
        self.assertEqual(get_plan_review(self.cli.output, self.directory.name), self.review)

    def test_cancel_at_a_pause_points_to_rejection_without_leaving_a_stop_marker(self):
        """There is no active worker to acknowledge cancellation during human review."""
        self.pause()
        result = self.command("cancel")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--reject", result.stderr)
        self.assertFalse((self.directory / "cancel.request").exists())

    def test_missing_checkpoint_is_not_reconstructed_from_display_json(self):
        """Old runs and copied snapshots cannot pretend to have native resumable history."""
        self.pause()
        store = self.cli.output / "workflow_checkpoints.sqlite"
        store.rename(self.cli.output / "test-hidden-checkpoint.sqlite")
        with self.assertRaisesRegex(ResearchError, "No resumable checkpoint"):
            get_plan_review(self.cli.output, self.directory.name)
        self.assertFalse(store.exists())
        self.assertFalse((self.directory / "documents.json").exists())

    def test_full_decision_file_uses_the_same_contract_as_a_nonterminal_adapter(self):
        """Machine-oriented CLI submission reaches the same validation and reject branch."""
        self.pause()
        path = self.cli.directory / "decision.json"
        path.write_text(self.decision("reject", note="Reject from a structured client.").model_dump_json())
        result = self.command("review", "--decision", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.directory / "session.json").read_text())["status"], "rejected")
        self.assertFalse((self.directory / "documents.json").exists())
