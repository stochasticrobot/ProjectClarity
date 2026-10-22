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


class FriendlyConsoleTests(unittest.TestCase):
    """Check both presentation modes without replacing graph nodes or model-message builders."""

    def setUp(self):
        """Reuse the offline two-round source/provider fixtures, not a demo-only workflow."""
        self.flow = loop_tests.ReviewDiscoveryIntegrationTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        status, headers, body = self.flow.routes[("example.org", "/research")]
        self.flow.routes[("example.org", "/research")] = (status, headers, body.replace(b"</main>", b"<p>PRIVATE_SOURCE_MARKER</p></main>"))

    def run_cli(self, *flags, provider=None):
        """Use default compact output unless the tested command explicitly requests details."""
        return self.flow.cli.run_cli(provider or self.flow.provider, "--extract", "--review-claims", "--follow-links",
            "--max-requests", "8", *flags, plan_only=False, source_dns=public_dns, direct_entry=True, details=False)

    def test_default_explains_actual_nodes_urls_results_and_stop_without_prompt_dump(self):
        """A newcomer sees work and outcomes, not raw event field names or a full graph dump."""
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        for message in ["Plan the research [plan_research]", "Retrieve sources [retrieve_sources]",
                "Downloading S1 (1/1): https://example.org/research", "Extract candidate knowledge [extract_knowledge]",
                "Review claim support [review_claims]", "Assign claim statuses [adjudicate_claims]",
                "Consider follow-up sources [select_followup_sources]", "Next: https://example.org/team",
                "2 supported by review", "no second hop will run"]:
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
        events = (directory / "events.jsonl").read_text()
        self.assertNotIn("PRIVATE_SOURCE_MARKER", events)
        self.assertNotIn("Alice", events)
        self.assertIn('"node_started"', events)

    def test_prompts_are_shown_before_actual_requests_and_replies_afterward_for_all_tasks(self):
        """Compare the human display with serialized provider input, keeping call counts unchanged."""
        observed = []
        def provider(request):
            """At the HTTP boundary, verify that the actual input was already displayed."""
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
        """Scripts still receive JSON events only; the human sink is not on this path."""
        code, _, err = self.run_cli("--progress-json")
        self.assertEqual(code, 0, err)
        events = [json.loads(line) for line in err.splitlines()]
        directory, _ = self.flow.cli.saved_session()
        self.assertEqual(events, [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()])
        self.assertNotIn("PRIVATE_SOURCE_MARKER", err)
        self.assertNotIn("https://example.org/research", err)

    def test_quiet_mode_has_summary_but_no_progress_or_private_messages(self):
        """Disabling progress still leaves an honest final outcome and inspection commands."""
        code, out, err = self.run_cli("--quiet-progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertIn("Research summary", out)
        self.assertIn("inspect-extraction", out)

    def test_details_flag_keeps_full_graph_and_plan_available(self):
        """The old detailed view is still reachable without changing research behavior."""
        code, out, err = self.run_cli("--details")
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded route", out)
        self.assertIn("## Proposed strategy", out)
        self.assertEqual(len(self.flow.cli.requests), 6)

    def test_bad_reply_is_visible_but_redacted_and_not_accepted(self):
        """Private diagnostics show a malformed reply without exposing the configured fake key."""
        self.flow.overrides["ClaimReviewBatch"] = lambda body: self.flow.cli.response(text=json.dumps({"bad_reply": TEST_KEY}))
        code, out, err = self.run_cli("--show-prompts")
        self.assertEqual(code, 1)
        self.assertIn("[REDACTED_API_KEY]", err)
        self.assertNotIn(TEST_KEY, out + err)
        self.assertIn("invalid_output_schema", err)
        self.assertIn("not yet validated", err)
        self.assertIn("needs review", out)

    def test_private_prompt_mode_cannot_be_combined_with_json_or_quiet(self):
        """Conflicting output choices are rejected before execution or credential access."""
        for other in ["--progress-json", "--quiet-progress"]:
            with self.subTest(other=other), patch("projectclarity.command_line.run_research") as run, \
                    patch("sys.stderr"):
                with self.assertRaises(SystemExit) as stopped:
                    main(["--question", "Example", "--show-prompts", other])
                self.assertEqual(stopped.exception.code, 2)
                run.assert_not_called()
