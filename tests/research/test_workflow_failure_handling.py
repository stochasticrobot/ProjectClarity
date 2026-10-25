"""Check graph-node decisions and interrupted work without calling external services.

For example, Ctrl-C after one retrieved source must keep its receipt and stop
before the next source. These tests call nodes directly, substitute only model
or source operations, and inspect real temporary snapshots. A failure here points
to workflow state handling; separate CLI integration tests check LangGraph wiring.
Saved snapshots are diagnostic records, not proof that resumable jobs exist.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from projectclarity.agentic_workflow import after_planning, plan_research, retrieve_sources
from projectclarity.research_schemas import RetrievedDocument, URLSource
from tests.research.research_test_fixtures import example_session


class WorkflowFailureHandlingTests(unittest.TestCase):
    """Test observable status, accounting, and retained output, not graph internals."""

    def setUp(self):
        """Create the output folder expected by directly called nodes, without starting a graph.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "offline-run").mkdir()

    def saved_session(self):
        """Read only the isolated run created for this test.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            The parsed session dictionary saved in this test's isolated offline-run directory.
        """
        return json.loads((self.root / "offline-run/session.json").read_text())

    def test_only_valid_non_plan_only_sessions_reach_review(self):
        """Unresolved plans go to review; failures and plan-only requests still stop.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        for status in ("plan_ready", "needs_clarification", "failed", "cancelled", "initialized"):
            for plan_only in (True, False):
                with self.subTest(status=status, plan_only=plan_only):
                    session = example_session(plan_only=plan_only)
                    session.status = status
                    expected = "review" if status in {"plan_ready", "needs_clarification"} and not plan_only else "stop"
                    self.assertEqual(after_planning({"session": session}), expected)

    def test_planning_cancellation_saves_status_and_correct_attempt_count(self):
        """An interrupted model call counts as attempted; interrupted key entry does not.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        for interruption in (KeyboardInterrupt, EOFError):
            for stage in ("key_entry", "model_call"):
                with self.subTest(interruption=interruption.__name__, stage=stage):
                    session = example_session(with_plan=False)
                    key_reader = Mock(return_value="synthetic-test-key")
                    planner = Mock()
                    if stage == "key_entry":
                        key_reader.side_effect = interruption()
                    else:
                        planner.invoke.side_effect = interruption()
                    with patch("projectclarity.nodes.plan_research.node.create_planner", return_value=planner) as create:
                        plan_research({"session": session}, self.root, key_reader=key_reader)
                    saved = self.saved_session()
                    self.assertEqual(saved["status"], "cancelled")
                    self.assertEqual(saved["usage"]["requests_attempted"], int(stage == "model_call"))
                    self.assertIsNone(saved["usage"]["total_tokens"])
                    self.assertIsNone(saved["plan"])
                    self.assertNotIn("synthetic-test-key", json.dumps(saved))
                    self.assertFalse((self.root / "offline-run/plan.json").exists())
                    if stage == "key_entry":
                        create.assert_not_called()

    def test_retrieval_cancellation_keeps_completed_receipts_and_stops_new_work(self):
        """A partially completed run remains inspectable after Ctrl-C or input closure.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        for interruption in (KeyboardInterrupt, EOFError):
            with self.subTest(interruption=interruption.__name__):
                session = example_session(plan_only=False)
                session.sources.extend([URLSource(source_id=f"S{number}", url=f"https://example.org/{number}")
                                        for number in (2, 3)])
                completed = RetrievedDocument(source_id="S1", source_kind="url",
                    original_location=session.sources[0].url, attempted_at=session.created_at,
                    status="retrieved", text_path="documents/S1/text.txt", character_count=10)
                with patch("projectclarity.nodes.retrieve_sources.node.retrieve_one",
                           side_effect=[completed, interruption()]) as retrieve:
                    retrieve_sources({"session": session}, self.root)
                saved = self.saved_session()
                manifest = json.loads((self.root / "offline-run/documents.json").read_text())
                self.assertEqual(saved["status"], "cancelled")
                self.assertEqual(saved["documents"], [completed.model_dump()])
                self.assertEqual(manifest["documents"], saved["documents"])
                self.assertEqual(retrieve.call_count, 2)  # S3 was never attempted.

    def test_unexpected_retrieval_failure_is_saved_without_exception_payload(self):
        """A worker-level failure is distinguishable from success and safe to inspect.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        session = example_session(plan_only=False)
        with patch("projectclarity.nodes.retrieve_sources.node.retrieve_one", side_effect=OSError("PRIVATE_PAYLOAD")):
            retrieve_sources({"session": session}, self.root)
        saved = self.saved_session()
        self.assertEqual(saved["status"], "retrieval_failed")
        self.assertIn("OSError", saved["error"])
        self.assertNotIn("PRIVATE_PAYLOAD", json.dumps(saved))
