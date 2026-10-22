"""Verify text graph views against real research execution, without live services.

The provider reply is synthetic, but the CLI, compiled graph, review checkpoints,
local text parsing and output files are real. A paused or failed node must not
look completed, and inspecting a graph must not start another model call. These
checks prove the present sequential route view, not a general tracing engine.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projectclarity import agentic_workflow
from projectclarity.command_line import main
from projectclarity.plan_review_contracts import PlanReviewDecision
from projectclarity.plan_review_service import get_plan_review, submit_plan_review
from projectclarity.run_graph_visualization import export_saved_run_graph, graph_definition
from projectclarity.session_storage import save_session
from tests.planning import test_planning_cli_integration as planning_tests
from tests.research.research_test_fixtures import example_session


class RunGraphVisualizationTests(unittest.TestCase):
    """Use production entry points and inspect persisted evidence, not just pretty text."""

    def new_run(self, *, plan_only=False, failed=False, questions=False):
        """Create one isolated real CLI run for a chosen planner outcome."""
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        document = cli.directory / "evidence.txt"
        document.write_text("PRIVATE_SOURCE_TEXT_FOR_RETRIEVAL\n")
        if not questions:
            cli.plan["clarifications"] = []
        if failed:
            del cli.plan["question_category"]
        captured = []
        build = agentic_workflow.build_graph

        def capture(*args, **kwargs):
            """Keep the actual compiled graph so saved topology can be compared to the graph invoked."""
            graph = build(*args, **kwargs)
            captured.append(graph)
            return graph

        with patch.object(agentic_workflow, "build_graph", side_effect=capture):
            code, stdout, stderr = cli.run_cli(lambda request: cli.response(),
                "--document", str(document), include_urls=False, plan_only=plan_only)
        directory, session = cli.saved_session()
        snapshot = json.loads((directory / "workflow_graph.json").read_text())
        text = (directory / "workflow_graph.txt").read_text()
        self.assertEqual(snapshot["definition"], graph_definition(captured[0]))
        self.assertEqual(snapshot["definition_origin"], "captured")
        self.assertEqual(snapshot["visited_nodes"], session["visited_nodes"])
        self.assertNotIn("PRIVATE_SOURCE_TEXT", text + json.dumps(snapshot))
        self.assertNotIn(str(document), text + json.dumps(snapshot))
        self.assertEqual(len(cli.requests), 1)
        return cli, directory, session, snapshot, text, code, stdout, stderr

    def test_completed_route_uses_the_invoked_graph_and_preserves_other_branches(self):
        """The actual compiled topology, not a hard-coded chain, supplies the diagram."""
        _, directory, _, snapshot, text, code, stdout, stderr = self.new_run()
        self.assertEqual(code, 0, stderr)
        self.assertTrue(snapshot["invocation_finished"])
        self.assertEqual(snapshot["sources_retrieved"], 1)
        self.assertIn("PRIVATE_SOURCE_TEXT", (directory / "documents/S1/text.txt").read_text())
        self.assertIn("[retrieve_sources]", text)
        self.assertIn("[END]", text)
        self.assertIn("passed through; no human approval", text)
        self.assertIn("Other available edges (not recorded as taken)", text)
        self.assertIn("plan_research --stop--> END", text)
        self.assertIn(text.strip(), stdout)

    def test_plan_only_failed_and_paused_runs_do_not_claim_retrieval(self):
        """END means the invocation returned; a paused review must not show END."""
        for options, expected_code, terminal in [({"plan_only": True}, 0, True),
                                                ({"failed": True}, 1, True),
                                                ({"questions": True}, 2, False)]:
            with self.subTest(options=options):
                _, _, _, snapshot, text, code, _, _ = self.new_run(**options)
                self.assertEqual(code, expected_code)
                self.assertEqual(snapshot["invocation_finished"], terminal)
                route = text.split("Other available edges")[0]
                self.assertNotIn("[retrieve_sources]", route)
                self.assertEqual("[END]" in route, terminal)
                if options.get("failed"):
                    self.assertIn("[plan_research] -- failed", route)
                if options.get("questions"):
                    self.assertIn("[review_plan] -- PAUSED for human input", route)

    def test_review_resume_updates_the_same_run_graph_without_replanning(self):
        """Both acceptance and rejection replace the paused view with the actual outcome."""
        for action in ("accept_assumptions", "reject"):
            with self.subTest(action=action):
                cli, directory, _, _, _, _, _, _ = self.new_run(questions=True)
                review = get_plan_review(cli.output, directory.name)
                decision = PlanReviewDecision(run_id=directory.name, review_id=review.review_id, action=action)
                with patch.object(agentic_workflow, "create_planner", side_effect=AssertionError("No model call")):
                    session = submit_plan_review(cli.output, decision)
                snapshot = json.loads((directory / "workflow_graph.json").read_text())
                text = (directory / "workflow_graph.txt").read_text()
                self.assertEqual(snapshot["human_decision"], action)
                self.assertTrue(snapshot["invocation_finished"])
                self.assertNotIn("PAUSED", text)
                self.assertEqual(snapshot["status"], session.status)
                self.assertEqual(snapshot["sources_retrieved"], int(action == "accept_assumptions"))
                self.assertEqual(session.usage.requests_attempted, 1)

    def test_graph_command_reads_capture_without_building_or_invoking_a_graph(self):
        """Inspecting a captured run is a local presentation operation only."""
        cli, directory, _, _, _, _, _, _ = self.new_run()
        output = io.StringIO()
        with patch.object(agentic_workflow, "build_graph", side_effect=AssertionError("Already captured")), \
             patch.object(agentic_workflow, "create_planner", side_effect=AssertionError("No model call")), \
             contextlib.redirect_stdout(output):
            code = main(["graph", directory.name, "--output-dir", str(cli.output)])
        self.assertEqual(code, 0)
        self.assertIn("[retrieve_sources]", output.getvalue())
        self.assertIn("Saved graph:", output.getvalue())
        self.assertEqual(len(cli.requests), 1)

    def test_older_run_is_explicitly_reconstructed_without_rewriting_session(self):
        """A diagram made now cannot pretend it captured a historical graph definition."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = example_session()
            directory = root / session.session_id
            directory.mkdir()
            save_session(session, root)
            before = (directory / "session.json").read_bytes()
            with patch.object(agentic_workflow, "create_planner", side_effect=AssertionError("No model call")):
                destination = export_saved_run_graph(root, session.session_id)
            self.assertIn("reconstructed from CURRENT code", destination.read_text())
            self.assertEqual((directory / "session.json").read_bytes(), before)

    def test_graph_command_rejects_invalid_run_paths_before_any_work(self):
        """Inspection uses the existing run-ID boundary, not arbitrary filesystem paths."""
        with contextlib.redirect_stderr(io.StringIO()), \
             patch.object(agentic_workflow, "build_graph", side_effect=AssertionError("No graph needed")):
            self.assertEqual(main(["graph", "../outside"]), 2)
