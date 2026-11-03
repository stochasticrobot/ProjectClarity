"""Check the standalone tool that connects answer requirements to fact extraction.

The tests supply known AI replies and run the real command-line entry point. They
check that extraction receives the actual requirements, document pieces remain
complete, failed planning prevents dependent work and previews make no requests.
All results are temporary; these tests check the tool's behaviour, not extraction accuracy.
"""

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from testing import extraction_test_rig
from projectclarity.question_definition.requirements_definition import validate_plan

PLAN = {"information_needed": [{"kind": "attribute", "name": "AI focus", "description": "Capture the company's AI work."}]}
EMPTY = {"entities": [], "facts": []}


def reply(payload, stop_reason="end_turn"):
    """Construct a fake SDK response for offline orchestration checks.

    Args:
        payload: JSON-serializable response body.
        stop_reason: Provider stop reason to simulate.

    Returns:
        Mock response exposing SDK-style text, stop reason and serialized metadata.
    """
    result = MagicMock()
    result.content = [SimpleNamespace(type="text", text=json.dumps(payload))]
    result.stop_reason = stop_reason
    result.model_dump.return_value = {"content": [{"type": "text", "text": json.dumps(payload)}], "stop_reason": stop_reason}
    return result


class RigContracts(unittest.TestCase):
    """Exercise the real CLI orchestration with provider calls replaced locally."""

    def execute(self, mode, responses, extra=()):
        """Run one CLI scenario in a temporary directory and retain its artifacts.

        Args:
            mode: CLI mode under test.
            responses: Ordered fake SDK responses for the mocked client.
            extra: Additional CLI arguments, appended after the question if needed.

        Returns:
            Exit code, client mock and decoded run artifacts. Temporary files are
            removed after reading; no network, old project access or real key is used.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "source.txt"
            document.write_text("Acme develops AI software.")
            args = [mode, str(document)] if mode != "plan" else [mode]
            if mode in ("plan", "combined"):
                args += ["--question", "List AI companies."]
            args += ["--output-root", str(root / "runs"), *extra]
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-secret"}), patch("testing.extraction_test_rig.Anthropic") as factory, contextlib.redirect_stdout(io.StringIO()):
                client = factory.return_value.__enter__.return_value
                client.messages.create.side_effect = responses
                code = extraction_test_rig.main(args)
            folder = next((root / "runs").iterdir())
            artifacts = {p.name: json.loads(p.read_text()) for p in folder.glob("*.json")}
            return code, client, artifacts

    def test_generic_extraction_does_not_plan(self):
        """Check generic mode makes one call and supplies no research requirements.

        Args: None.
        Returns: None; assertion failures report routing or prompt-copy regressions.
        """
        code, client, artifacts = self.execute("extract", [reply(EMPTY)])
        self.assertEqual(code, 0)
        self.assertEqual(client.messages.create.call_count, 1)
        request = client.messages.create.call_args.kwargs
        self.assertEqual(json.loads(request["messages"][0]["content"])["requested_information"], [])
        self.assertEqual(request["system"], (extraction_test_rig.ROOT / "src/projectclarity/prompts/structured_information_extraction.txt").read_text())
        self.assertNotIn("plan.json", artifacts)

    def test_combined_handoff_is_exact(self):
        """Verify the actual planner result, not a fixture or copied default, is forwarded.

        Args: None.
        Returns: None; asserts ordered calls, exact data handoff and reusable output.
        """
        code, client, artifacts = self.execute("combined", [reply(PLAN), reply(EMPTY)])
        self.assertEqual(code, 0)
        self.assertEqual(client.messages.create.call_count, 2)
        extraction = client.messages.create.call_args_list[1].kwargs
        data = json.loads(extraction["messages"][0]["content"])
        self.assertEqual(data["requested_information"], PLAN["information_needed"])
        self.assertEqual(artifacts["requirements.json"]["information_needed"], PLAN["information_needed"])
        self.assertEqual(artifacts["summary.json"]["attempted_calls"], 2)

    def test_failed_planner_blocks_extraction(self):
        """Ensure an invalid planner response cannot silently become generic extraction.

        Args: None.
        Returns: None; verifies one attempt and no accepted requirements or chunks.
        """
        code, client, artifacts = self.execute("combined", [reply({"information_needed": []})])
        self.assertEqual(code, 1)
        self.assertEqual(client.messages.create.call_count, 1)
        self.assertNotIn("chunk-0000.json", artifacts)
        self.assertNotIn("requirements.json", artifacts)

    def test_one_plan_is_shared_across_all_chunks(self):
        """Check variable-size extraction uses one plan without losing source text.

        Args: None.
        Returns: None; asserts one planning call followed by three matching handoffs.
        """
        code, client, _ = self.execute("combined", [reply(PLAN), reply(EMPTY), reply(EMPTY), reply(EMPTY)], ["--chunk-chars", "12"])
        self.assertEqual(code, 0)
        calls = client.messages.create.call_args_list
        self.assertEqual(len(calls), 4)
        payloads = [json.loads(call.kwargs["messages"][0]["content"]) for call in calls[1:]]
        self.assertTrue(all(p["requested_information"] == PLAN["information_needed"] for p in payloads))
        self.assertEqual("".join(p["document_chunk"] for p in payloads), "Acme develops AI software.")

    def test_plan_only_writes_reusable_requirements(self):
        """Check Task 1 can run without any extraction document or call.

        Args: None.
        Returns: None; verifies a single planner call and a saved requirements file.
        """
        code, client, artifacts = self.execute("plan", [reply(PLAN)])
        self.assertEqual(code, 0)
        self.assertEqual(client.messages.create.call_count, 1)
        self.assertEqual(artifacts["requirements.json"]["information_needed"], PLAN["information_needed"])
        self.assertNotIn("chunk-0000.json", artifacts)

    def test_truncated_planner_blocks_extraction(self):
        """Reject truncated planning even when its visible JSON happens to be valid.

        Args: None.
        Returns: None; verifies provider stop reason is enforced without retry.
        """
        code, client, _ = self.execute("combined", [reply(PLAN, "max_tokens")])
        self.assertEqual(code, 1)
        self.assertEqual(client.messages.create.call_count, 1)

    def test_saved_requirements_are_reusable(self):
        """Check a saved requirements artifact bypasses planning without losing content.

        Args: None.
        Returns: None; temporary metadata-bearing file is accepted and forwarded.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            path.write_text(json.dumps(dict(PLAN, credential_redaction_applied=False)))
            code, client, _ = self.execute("extract", [reply(EMPTY)], ["--requirements", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(client.messages.create.call_count, 1)
            self.assertEqual(json.loads(client.messages.create.call_args.kwargs["messages"][0]["content"])["requested_information"], PLAN["information_needed"])

    def test_dry_run_never_reads_secrets_or_calls_provider(self):
        """Prove combined preview stays offline and does not fabricate planner output.

        Args: None.
        Returns: None; asserts the preview marks extraction inputs as pending.
        """
        output = io.StringIO()
        with patch("testing.extraction_test_rig.Anthropic") as client, patch("testing.extraction_test_rig.dotenv_values") as secrets, contextlib.redirect_stdout(output):
            code = extraction_test_rig.main(["combined", str(extraction_test_rig.ROOT / "tests/fixtures/business_excerpt.txt"), "--question", "List companies.", "--dry-run"])
        self.assertEqual(code, 0)
        client.assert_not_called()
        secrets.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["extraction_requests"], [])

    def test_total_budget_checked_before_planning(self):
        """Ensure the call cap includes planning and rejects impossible runs up front.

        Args: None.
        Returns: None; verifies no provider initialization for an insufficient budget.
        """
        with patch("testing.extraction_test_rig.Anthropic") as client, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                extraction_test_rig.main(["combined", str(extraction_test_rig.ROOT / "tests/fixtures/business_excerpt.txt"), "--question", "List companies.", "--max-calls", "1"])
        client.assert_not_called()

    def test_chunk_offsets_preserve_documents_and_empty_input(self):
        """Verify variable chunks retain exact text, source boundaries and empty files.

        Args: None.
        Returns: None; asserts all source characters survive once in the right order.
        """
        with tempfile.TemporaryDirectory() as directory:
            first, empty = Path(directory) / "first.txt", Path(directory) / "empty.txt"
            first.write_text("αβγ\n123"); empty.write_text("")
            chunks = extraction_test_rig.prepare_chunks([first, empty], 3)
            self.assertEqual("".join(c["text"] for c in chunks[:-1]), first.read_text())
            self.assertEqual([(c["start_char"], c["end_char"]) for c in chunks[:-1]], [(0, 3), (3, 6), (6, 7)])
            self.assertEqual(chunks[-1]["text"], "")


if __name__ == "__main__":
    unittest.main()
