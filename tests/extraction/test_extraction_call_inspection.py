"""Check that a developer can see what extraction actually asked and received.

Integration cases run the real terminal entry point, graph and SDK with invented HTTP answers. They
compare the private trace to the SDK request, exercise the inspection command,
and ensure failed answers remain inspectable without leaking into event logs.
No live model or developer credential is used. Historical reconstruction is
tested separately: matching input hashes do not recover original answer text.
One direct helper check covers omission of non-answer blocks and capture limits;
it does not exercise a model or the full graph.
"""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx2
from langchain_core.messages import AIMessage

from projectclarity.command_line import main
from projectclarity.extraction.extraction_call_tracing import capture_answer
from projectclarity.extraction.extraction_inspection import inspect_extraction
from projectclarity.nodes.plan_research.research_planner import ResearchError
from tests.extraction import test_knowledge_extraction_integration as integration
from tests.extraction.extraction_test_fixtures import proposed_batch
from tests.planning import test_planning_cli_integration as planning


class ExtractionCallInspectionTests(unittest.TestCase):
    """Use the shared offline production-path harness, not a second workflow."""

    def setUp(self):
        """Create an isolated extraction terminal interface harness so inspection never touches real runs.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
        self.harness = integration.KnowledgeExtractionIntegrationTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_trace_precedes_call_and_matches_serialized_messages_and_answer(self):
        """Inspect the prepared trace inside the fake server before returning its answer.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        answer = json.dumps(proposed_batch())

        def respond(request):
            """Compare the pre-call trace with the real SDK request before returning a synthetic
            answer.

            Args:
                request: Actual httpx2.Request emitted by the SDK; inspected locally and answered
                    with fixture data.

            Returns:
                A synthetic httpx2.Response passed through the real provider SDK; no network request
                is made by this responder.
            """
            directory, _ = self.harness.cli.saved_session()
            trace = json.loads((directory / "extraction_calls/S1_P1.json").read_text())
            wire = json.loads(request.content)
            self.assertEqual(trace["status"], "prepared")
            self.assertEqual(trace["messages"][0]["content"], wire["system"])
            self.assertEqual(trace["messages"][1]["content"], wire["messages"][0]["content"])
            self.assertEqual(trace["output_schema"]["title"], "ExtractionBatch")
            return self.harness.cli.response(text=answer)

        code, stdout, stderr, directory, _ = self.harness.run_extraction(provider=respond)
        self.assertEqual(code, 0, stderr)
        path = directory / "extraction_calls/S1_P1.json"
        trace = json.loads(path.read_text())
        self.assertEqual(trace["response_text"], answer)
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("Entities: Alice", stdout)
        self.assertIn("S1_P1:", stderr)
        self.assertIn("1 candidate claims", stderr)
        self.assertIn("Tokens: 150 in", stderr)
        self.assertNotIn("Alice", (directory / "events.jsonl").read_text())

    def test_failed_schema_answer_is_redacted_and_failure_visible(self):
        """Malformed model text is retained privately, with known credentials removed.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        answer = json.dumps({"unexpected": "bad answer " + planning.TEST_KEY})
        code, _, stderr, directory, _ = self.harness.run_extraction(
            provider=lambda request: self.harness.cli.response(text=answer))
        self.assertEqual(code, 1)
        trace = json.loads((directory / "extraction_calls/S1_P1.json").read_text())
        self.assertTrue(trace["credential_redaction_applied"])
        self.assertIn("[REDACTED_API_KEY]", trace["response_text"])
        self.assertIsNone(trace["parsed_result"])
        self.assertIn("failed —", stderr)
        self.assertIn("invalid_extraction_schema", stderr)
        self.assertNotIn("bad answer", (directory / "events.jsonl").read_text())

    def test_provider_failure_retains_private_body_without_public_leak(self):
        """Retain provider failures privately without leaking them into ordinary artifacts.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        reply = httpx2.Response(429, json={"type": "error", "error": {
            "type": "rate_limit_error", "message": "PRIVATE_PROVIDER_ERROR_BODY"}})
        code, _, _, directory, _ = self.harness.run_extraction(provider=lambda request: reply)
        self.assertEqual(code, 1)
        trace = json.loads((directory / "extraction_calls/S1_P1.json").read_text())
        self.assertEqual(trace["error_code"], "provider_or_adapter_error")
        self.assertIsNone(trace["response_text"])
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        finished = next(e for e in events if e["event"] == "extraction_call_finished")
        self.assertEqual(finished["level"], "error")
        receipts = list((directory / "provider_errors").glob("*.json"))
        self.assertTrue(receipts, "Provider error body must never be discarded.")
        for path in receipts:
            receipt = json.loads(path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(any(row.get("response_body") == reply.text
                                for row in receipt["errors"]))
            self.assertTrue(any("PRIVATE_PROVIDER_ERROR_BODY" in row["message"]
                                for row in receipt["errors"]))
        self.assertEqual((directory / "provider_errors").stat().st_mode & 0o777, 0o700)
        for path in directory.rglob("*"):
            if path.is_file() and path not in receipts:
                self.assertNotIn(b"PRIVATE_PROVIDER_ERROR_BODY", path.read_bytes())

    def test_inspection_cli_reads_saved_call_without_provider_or_session_mutation(self):
        """Inspecting an existing run is free and does not approve or change its proposals.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        _, _, _, directory, session = self.harness.run_extraction()
        before = (directory / "session.json").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch(
            "projectclarity.nodes.plan_research.research_planner.read_api_key", side_effect=AssertionError("No key needed")
        ), patch("socket.socket.connect", side_effect=AssertionError("No network needed")):
            code = main(["inspect-extraction", session["session_id"], "--call", "S1_P1",
                         "--output-dir", str(directory.parent)])
        self.assertEqual(code, 0)
        self.assertIn("SYSTEM MESSAGE", output.getvalue())
        self.assertIn("MODEL ANSWER TEXT", output.getvalue())
        self.assertIn("Alice", output.getvalue())
        self.assertEqual(before, (directory / "session.json").read_bytes())
        self.assertEqual((directory / "extraction_inspection_S1_P1.txt").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ResearchError):
            inspect_extraction(directory.parent, session["session_id"], "../../escape")

    def test_historical_inputs_require_matching_hash_and_never_invent_reply(self):
        """Older runs can reconstruct inputs only while their recorded source still matches.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        _, _, _, directory, session = self.harness.run_extraction()
        trace = directory / "extraction_calls/S1_P1.json"
        trace.rename(trace.with_suffix(".held"))
        output, _ = inspect_extraction(directory.parent, session["session_id"], "S1_P1")
        self.assertIn("fingerprint matches", output)
        self.assertIn("parsed result below is not the original text", output)
        (directory / "documents/S1/text.txt").write_text("Changed source")
        output, _ = inspect_extraction(directory.parent, session["session_id"], "S1_P1")
        self.assertIn("Input: unavailable", output)

    def test_entities_without_claims_are_not_presented_as_empty_extraction(self):
        """A recognized name and a supported claim are separate useful outcomes.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        batch = proposed_batch()
        batch["claims"] = []
        code, stdout, _, _, _ = self.harness.run_extraction(batch=batch)
        self.assertEqual(code, 0)
        self.assertIn("Names were extracted, but no claim was proposed", stdout)
        self.assertNotIn("Model returned empty lists", stdout)

    def test_capture_omits_non_answer_blocks_and_labels_size_limit(self):
        """Do not turn provider reasoning/signatures into an inspectable answer dump.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        trace = {}
        capture_answer(trace, AIMessage(content=[
            {"type": "thinking", "thinking": "NOT_ANSWER", "signature": "PRIVATE"},
            {"type": "text", "text": "x" * 128_001}]))
        self.assertTrue(trace["non_text_blocks_omitted"])
        self.assertTrue(trace["response_text_truncated"])
        self.assertEqual(len(trace["response_text"]), 128_000)
        self.assertNotIn("NOT_ANSWER", json.dumps(trace))
