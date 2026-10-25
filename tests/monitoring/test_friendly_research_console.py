"""Verify human CLI explanations and private prompt display through real production entry points.

The existing integration harness substitutes only source/provider HTTP and DNS;
our CLI, graph, message builders, SDK serialization, parser and persistence run.
Tests compare displayed input with the actual provider request and ensure JSON
telemetry never receives source text. No live services or real credentials are used.
"""
import json
import sys
import unittest
from unittest.mock import patch

from tests.claim_review import test_review_and_discovery_integration as loop_tests
from tests.retrieval.retrieval_test_fixtures import public_dns
from tests.planning.test_planning_cli_integration import TEST_KEY
from projectclarity.command_line import main
from projectclarity.terminal_run_progress import ResearchConsole
from projectclarity.research_schemas import ResearchRequest


class FriendlyConsoleTests(unittest.TestCase):
    """Check both presentation modes without replacing graph nodes or model-message builders."""

    def setUp(self):
        """Reuse the offline two-round source/provider fixtures, not a demo-only workflow.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
        self.flow = loop_tests.ReviewDiscoveryIntegrationTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        status, headers, body = self.flow.routes[("example.org", "/research")]
        self.flow.routes[("example.org", "/research")] = (status, headers, body.replace(b"</main>", b"<p>PRIVATE_SOURCE_MARKER</p></main>"))

    def run_cli(self, *flags, provider=None):
        """Use compact output with an explicit one-hop/origin scope for this fixed six-call fixture.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            provider: Optional fake HTTP responder; None uses the shared integration harness's
                task router.
            *flags: Additional command-line argument strings appended to this helper's base
                invocation.

        Returns:
            A tuple of CLI exit code, captured stdout and captured stderr for the
            extraction/review/discovery fixture.
        """
        return self.flow.cli.run_cli(provider or self.flow.provider, "--extract", "--review-claims", "--follow-links",
            "--max-link-depth", "1", "--same-origin-links", "--max-requests", "8", *flags,
            plan_only=False, source_dns=public_dns, direct_entry=True, details=False)

    def test_default_explains_actual_nodes_urls_results_and_stop_without_prompt_dump(self):
        """A newcomer sees work and outcomes, not raw event field names or a full graph dump.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        for message in ["Plan the research [plan_research]", "Retrieve sources [retrieve_sources]",
                "Downloading S1 (1/1): https://example.org/research", "Extract candidate knowledge [extract_knowledge]",
                "Review claim support [review_claims]", "Assign claim statuses [adjudicate_claims]",
                "Consider follow-up sources [select_followup_sources]", "Next: https://example.org/team",
                "2 supported by review", "configured link-depth limit was reached"]:
            self.assertIn(message, err)
        self.assertIn("Follow-up links: 1 selected; 1 downloaded.", out)
        self.assertIn("Model requests: 6/8", out)
        self.assertNotIn("Recorded route", out)
        self.assertNotIn("## Proposed strategy", out)
        self.assertNotIn("stage=", err)
        self.assertNotIn("MODEL INPUT", err)
        self.assertNotIn("PRIVATE_SOURCE_MARKER", out + err)
        directory, session = self.flow.cli.saved_session()
        self.assertEqual(len(self.flow.cli.requests), 6)
        self.assertEqual(session["visited_nodes"].count("retrieve_sources"), 2)
        # Verify delivery through the actual CLI, including both retrieval rounds.
        self.assertIn("retrieval wall time: not started", err)
        self.assertIn("model requests: 6/8 (2 left)", err)
        self.assertIn("Pages/documents registered: 2/250 (248 slots left)", err)
        self.assertIn("website hostnames: 1", err)
        self.assertIn("pages retrieved: 2, failed: 0", err)
        self.assertIn("HTTP attempts:", err)
        self.assertIn("no run-wide token cap", err)
        self.assertIn(f"Reported tokens: {session['usage']['total_tokens']:,}", err)
        events = (directory / "events.jsonl").read_text()
        self.assertNotIn("PRIVATE_SOURCE_MARKER", events)
        self.assertNotIn("Alice", events)
        self.assertIn('"node_started"', events)

    def test_budget_clock_includes_model_time_without_restarting_each_round(self):
        """Check clock semantics and missing token honesty with controlled event timing.

        This supplements the real-CLI check above; it does not claim live-network
        timing accuracy or exercise the retrieval enforcement mechanism.

        Args:
            self: Test case using an isolated console and deterministic monotonic time.

        Returns:
            None. Fail if later retrieval rounds replenish time, remaining time becomes
            negative, or unknown usage is displayed as zero tokens.
        """
        request = ResearchRequest(question="Compare generic alternatives.").model_dump(mode="json")
        request["fetch_limits"]["max_run_seconds"] = 20
        session = {"request": request, "usage": {"total_tokens": None}}
        console = ResearchConsole(self.flow.cli.directory / "missing-output")
        lines = []
        with patch.object(console, "session", return_value=session), \
                patch.object(console, "say", side_effect=lines.append), \
                patch("projectclarity.terminal_run_progress.time.monotonic") as now:
            event = {"run_id": "clock-check", "event": "retrieval_started", "details": {}}
            now.return_value = 100
            console(event)
            now.return_value = 115
            console(event)  # Another round must not restart the shared clock.
            now.return_value = 130
            console.budget_status()
        display = "\n".join(lines)
        self.assertIn("30/20s (0s left)", display)
        self.assertIn("Reported tokens: unknown/incomplete", display)

    def test_prompts_are_shown_before_actual_requests_and_replies_afterward_for_all_tasks(self):
        """Compare the human display with serialized provider input, keeping call counts unchanged.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        observed = []
        def provider(request):
            """At the HTTP boundary, verify that the actual input was already displayed.

            Args:
                request: Actual httpx2.Request emitted by the SDK; inspected locally and answered
                    with fixture data.

            Returns:
                A synthetic httpx2.Response passed through the real provider SDK; no network request
                is made by this responder.
            """
            wire = json.loads(request.content)
            prompt = wire["system"]
            displayed = sys.stderr.getvalue()
            self.assertIn(prompt, displayed)
            # Compare the whole task payload, not merely its system instructions.
            # The terminal moves passage text below pretty JSON for readability;
            # reconstruct that presentation and require every provider field.
            task = json.loads(wire["messages"][0]["content"])
            human_display = displayed.rsplit("HUMAN MESSAGE\n", 1)[1]
            rendered, end = json.JSONDecoder().raw_decode(human_display)
            if isinstance(task.get("passage"), dict):
                passage_block = human_display[end:].split("SOURCE PASSAGE ", 1)[1]
                passage_id, passage_text = passage_block.split("\n", 1)
                self.assertEqual(passage_id, task["passage"]["passage_id"])
                passage_text = passage_text.split("\n  --- END MODEL INPUT ---", 1)[0]
                rendered["passage"]["text"] = passage_text
            self.assertEqual(rendered, task)
            observed.append(wire["output_config"]["format"]["schema"]["title"])
            return self.flow.provider(request)
        code, _, err = self.run_cli("--show-prompts", provider=provider)
        self.assertEqual(code, 0, err)
        self.assertEqual(observed, ["ResearchPlan", "ExtractionBatch", "ClaimReviewBatch", "FollowupPlan", "ExtractionBatch", "ClaimReviewBatch"])
        self.assertIn("PRIVATE_SOURCE_MARKER", err)
        self.assertIn("MODEL REPLY · plan_research · plan (not yet validated)", err)
        self.assertIn("MODEL REPLY · extract_knowledge · S1_P1", err)
        self.assertIn('"assessments"', err)
        self.assertIn("review_S2_P1", err)
        directory, _ = self.flow.cli.saved_session()
        self.assertNotIn("PRIVATE_SOURCE_MARKER", (directory / "events.jsonl").read_text())
        self.assertNotIn("PRIVATE_SOURCE_MARKER", (directory / "run_status.json").read_text())

    def test_json_progress_remains_identical_to_saved_content_free_events(self):
        """Scripts still receive JSON events only; the human sink is not on this path.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, _, err = self.run_cli("--progress-json")
        self.assertEqual(code, 0, err)
        events = [json.loads(line) for line in err.splitlines()]
        directory, _ = self.flow.cli.saved_session()
        self.assertEqual(events, [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()])
        self.assertNotIn("PRIVATE_SOURCE_MARKER", err)
        self.assertNotIn("https://example.org/research", err)

    def test_quiet_mode_has_summary_but_no_progress_or_private_messages(self):
        """Disabling progress still leaves an honest final outcome and inspection commands.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, err = self.run_cli("--quiet-progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("Research summary", out)
        self.assertIn("inspect-extraction", out)

    def test_details_flag_keeps_full_graph_and_plan_available(self):
        """The old detailed view is still reachable without changing research behavior.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        code, out, err = self.run_cli("--details")
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded route", out)
        self.assertIn("## Proposed strategy", out)
        self.assertEqual(len(self.flow.cli.requests), 6)

    def test_bad_reply_is_visible_but_redacted_and_not_accepted(self):
        """Private diagnostics show a malformed reply without exposing the configured fake key.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.flow.overrides["ClaimReviewBatch"] = lambda body: self.flow.cli.response(text=json.dumps({"bad_reply": TEST_KEY}))
        code, out, err = self.run_cli("--show-prompts")
        self.assertEqual(code, 1)
        self.assertIn("[REDACTED_API_KEY]", err)
        self.assertNotIn(TEST_KEY, out + err)
        self.assertIn("invalid_output_schema", err)
        self.assertIn("not yet validated", err)
        self.assertIn("needs review", out)

    def test_private_prompt_mode_cannot_be_combined_with_json_or_quiet(self):
        """Conflicting output choices are rejected before execution or credential access.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        for other in ["--progress-json", "--quiet-progress"]:
            with self.subTest(other=other), patch("projectclarity.command_line.run_research") as run, \
                    patch("sys.stderr"):
                with self.assertRaises(SystemExit) as stopped:
                    main(["--question", "Example", "--show-prompts", other])
                self.assertEqual(stopped.exception.code, 2)
                run.assert_not_called()
