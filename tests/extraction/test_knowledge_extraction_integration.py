"""Run extraction through the same CLI, LangGraph and SDK used by the developer.

Normally only the provider HTTP reply is synthetic. Real local documents pass through
retrieval, the separately defined extraction node, evidence validation and saved
candidate output. Tests inspect requests, source spans, budgets, monitoring and
failure receipts—not just whether a helper function returned the expected text.
All credentials are invented and real sockets are denied by the shared harness.
These checks prove application behavior, not live model extraction accuracy.
The review-resume case substitutes the extraction adapter itself to inspect
human-guidance handoff; source-change cases wrap retrieval or edit saved fixtures.
Those narrower substitutions are explicit and are not provider-wire proofs.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from langchain_core.messages import AIMessage

from projectclarity import agentic_workflow
from projectclarity.extraction.extraction_contracts import ExtractionBatch
from projectclarity.plan_review_contracts import PlanReviewDecision, ReviewAnswer
from projectclarity.plan_review_service import get_plan_review, submit_plan_review
from projectclarity.research_schemas import ResearchSession
from tests.extraction.extraction_test_fixtures import SOURCE_TEXT, proposed_batch
from tests.planning import test_planning_cli_integration as planning_tests


class KnowledgeExtractionIntegrationTests(unittest.TestCase):
    """Use production entry points, keeping network/model substitutions explicit."""

    def setUp(self):
        """Prepare a temporary source and a valid no-clarification plan for extraction checks."""
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan["clarifications"] = []
        self.document = self.cli.directory / "research.txt"
        self.document.write_text(SOURCE_TEXT)

    def run_extraction(self, *flags, batch=None, provider=None, budget=2):
        """Supply a plan first, then extraction responses through real SDK serialization."""
        def response(request):
            """Return the planning fixture first, then the chosen extraction answer or failure."""
            if len(self.cli.requests) == 1:
                return self.cli.response()
            if provider:
                return provider(request)
            return self.cli.response(text=json.dumps(proposed_batch() if batch is None else batch))
        result = self.cli.run_cli(response, "--document", str(self.document), "--extract",
            "--max-requests", str(budget), *flags, include_urls=False, plan_only=False, direct_entry=True)
        directory, session = self.cli.saved_session()
        return (*result, directory, session)

    def test_cli_invokes_separate_nodes_with_plan_goals_and_saves_anchored_candidates(self):
        """The real graph uses the standalone node and preserves source/goal/evidence links."""
        code, stdout, stderr, directory, session = self.run_extraction()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(agentic_workflow.extract_knowledge.__module__, "projectclarity.extraction.extract_knowledge_node")
        self.assertEqual(session["visited_nodes"][-3:], ["extract_knowledge", "validate_evidence", "persist_candidates"])
        self.assertEqual(session["status"], "candidates_saved")
        self.assertEqual(session["usage"]["requests_attempted"], 2)
        self.assertEqual(session["usage"]["input_tokens"], 300)
        self.assertEqual(session["usage"]["output_tokens"], 440)
        request = json.loads(self.cli.requests[1].content)
        payload = json.loads(request["messages"][0]["content"])
        self.assertEqual(payload["goals"], self.cli.plan["goals"])
        self.assertEqual(payload["scope_in"], self.cli.plan["scope_in"])
        saved_text = (directory / "documents/S1/text.txt").read_text()
        self.assertEqual(payload["passage"]["text"], saved_text)
        self.assertNotIn(str(self.document), self.cli.requests[1].content.decode())
        self.assertNotIn("tools", request)
        self.assertEqual(request["output_config"]["format"]["schema"]["title"], "ExtractionBatch")
        candidate = session["extraction"]["candidates"][0]
        evidence = candidate["evidence"][0]
        self.assertEqual(saved_text[evidence["start"]:evidence["end"]], evidence["quote"])
        self.assertEqual(candidate["text_sha256"], session["documents"][0]["text_sha256"])
        self.assertEqual(candidate["review_status"], "unreviewed")
        self.assertEqual(candidate["publication_status"], "not_published")
        self.assertIn("Candidate claims and passages:", stdout)
        self.assertIn("Alice works at Lab X", (directory / "candidates.md").read_text())
        self.assertIn("[extract_knowledge]", (directory / "workflow_graph.txt").read_text())
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(status["metrics"]["candidate_count"], 1)
        events = (directory / "events.jsonl").read_text()
        self.assertIn('"extraction_call_started"', events)
        self.assertNotIn("Alice", events)
        self.assertEqual(json.loads((directory / "extraction_config.json").read_text())["max_retries"], 0)

    def test_fake_quotes_are_retained_as_invalid_not_published_or_successful(self):
        """A well-shaped reply with invented evidence produces a visible partial failure."""
        batch = proposed_batch()
        batch["claims"][0]["evidence"][0]["quote"] = "Alice heads the national AI agency."
        code, _, _, directory, session = self.run_extraction(batch=batch)
        self.assertEqual(code, 1)
        self.assertEqual(session["status"], "extraction_partial")
        candidate = session["extraction"]["candidates"][0]
        self.assertEqual(candidate["validation_errors"], ["quote_not_found"])
        self.assertEqual(candidate["publication_status"], "not_published")
        self.assertIn("quote_not_found", (directory / "candidates.md").read_text())

    def test_budget_exhaustion_keeps_later_passages_visible_and_never_retries(self):
        """Two allowed total calls means planning plus one passage, not a complete long document."""
        self.document.write_text(SOURCE_TEXT + ("background " * 2500))
        code, _, _, _, session = self.run_extraction(batch={"entities": [], "claims": []})
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cli.requests), 2)
        attempts = session["extraction"]["attempts"]
        self.assertGreater(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertTrue(all(a["status"] == "not_attempted" and a["error_code"] == "request_limit" for a in attempts[1:]))
        text = (self.cli.saved_session()[0] / "documents/S1/text.txt").read_text()
        self.assertEqual(attempts[-1]["end"], len(text))

    def test_empty_findings_are_distinct_from_failure(self):
        """A completed no-findings reply is recorded without inventing a claim or a gap verdict."""
        code, _, _, directory, session = self.run_extraction(batch={"entities": [], "claims": []})
        self.assertEqual(code, 0)
        self.assertEqual(session["extraction"]["candidates"], [])
        self.assertEqual(session["extraction"]["attempts"][0]["status"], "completed")
        self.assertIn("not proof", (directory / "candidates.md").read_text())

    def test_schema_failure_keeps_answer_in_private_trace_not_operational_logs(self):
        """A malformed answer is inspectable privately, but is not a claim or log dump."""
        code, _, _, directory, session = self.run_extraction(
            provider=lambda request: self.cli.response(text='{"private_bad_field":"UNTRUSTED_RAW_REPLY"}'))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cli.requests), 2)
        self.assertEqual(session["extraction"]["attempts"][0]["error_code"], "invalid_extraction_schema")
        self.assertEqual(session["extraction"]["candidates"], [])
        trace = directory / "extraction_calls/S1_P1.json"
        self.assertIn("UNTRUSTED_RAW_REPLY", trace.read_text())
        for path in directory.rglob("*"):
            if path.is_file() and path != trace:
                self.assertNotIn(b"UNTRUSTED_RAW_REPLY", path.read_bytes())

    def test_provider_error_stops_remaining_calls_and_marks_usage_unknown(self):
        """A 429 does not cause automatic retries or a series of new passage calls."""
        self.document.write_text(SOURCE_TEXT + "background " * 2500)
        response = httpx2.Response(429, json={"type": "error", "error": {
            "type": "rate_limit_error", "message": planning_tests.TEST_KEY}})
        code, _, _, _, session = self.run_extraction(provider=lambda request: response, budget=4)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cli.requests), 2)
        self.assertIsNone(session["usage"]["total_tokens"])
        self.assertEqual(session["extraction"]["attempts"][1]["error_code"], "stopped_after_provider_error")

    def test_token_limit_rejects_even_a_parseable_incomplete_reply(self):
        """Syntactically valid JSON is not a finished extraction when the provider reports truncation."""
        code, _, _, _, session = self.run_extraction(provider=lambda request:
            self.cli.response(text=json.dumps(proposed_batch()), stop_reason="max_tokens"))
        self.assertEqual(code, 1)
        self.assertEqual(session["extraction"]["attempts"][0]["error_code"], "incomplete_model_output")
        self.assertEqual(session["extraction"]["candidates"], [])
        self.assertEqual(len(self.cli.requests), 2)

    def test_source_changed_after_model_call_invalidates_the_candidate(self):
        """Validation must use the captured document version, not a subsequently edited file."""
        def response(request):
            """Modify saved text while the model responds to test the later source-version check."""
            directory, _ = self.cli.saved_session()
            (directory / "documents/S1/text.txt").write_text("Changed after extraction.")
            return self.cli.response(text=json.dumps(proposed_batch()))
        code, _, _, _, session = self.run_extraction(provider=response)
        self.assertEqual(code, 1)
        self.assertIn("source_version_mismatch", session["extraction"]["candidates"][0]["validation_errors"])

    def test_changed_source_before_extraction_never_reaches_provider(self):
        """A modified acquisition artifact fails before extraction, while telemetry still counts planning."""
        retrieve = agentic_workflow.retrieve_sources
        def change_after_retrieval(state, output_root, monitor=None, runtime=None):
            """Run real retrieval, then alter its text before extraction can send a request."""
            result = retrieve(state, output_root, monitor, runtime)
            session = result["session"]
            (output_root / session.session_id / "documents/S1/text.txt").write_text("Changed before extraction.")
            return result
        with patch.object(agentic_workflow, "retrieve_sources", side_effect=change_after_retrieval):
            code, _, _, directory, session = self.run_extraction()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.cli.requests), 1)
        self.assertEqual(session["extraction"]["source_errors"][0]["error_code"], "source_version_mismatch")
        status = json.loads((directory / "run_status.json").read_text())
        self.assertEqual(status["metrics"]["llm_requests"], 1)

    def test_cancel_after_response_keeps_finished_work_without_starting_next_call(self):
        """Cooperative cancellation retains completed candidates and stops additional spending."""
        self.document.write_text(SOURCE_TEXT + "background " * 2500)
        def response(request):
            """Request cancellation as an answer arrives so later passages must remain unattempted."""
            directory, _ = self.cli.saved_session()
            (directory / "cancel.request").write_text("{}")
            return self.cli.response(text=json.dumps(proposed_batch()))
        code, _, _, directory, session = self.run_extraction(provider=response, budget=4)
        self.assertEqual(code, 130)
        self.assertEqual(session["status"], "cancelled")
        self.assertEqual(len(self.cli.requests), 2)
        self.assertEqual(len(session["extraction"]["candidates"]), 1)
        self.assertTrue((directory / "candidates.md").exists())

    def test_plan_review_resume_preserves_extraction_opt_in_and_passes_human_guidance(self):
        """Answering a paused plan resumes into extraction without another planning request."""
        self.cli.plan["clarifications"] = ["Which interpretation should guide this run?"]
        code, _, _, directory, _ = self.run_extraction()
        self.assertEqual(code, 2)
        self.assertEqual(len(self.cli.requests), 1)
        review = get_plan_review(self.cli.output, directory.name)
        decision = PlanReviewDecision(run_id=directory.name, review_id=review.review_id, action="answer",
            answers=[ReviewAnswer(question_id=review.questions[0].question_id, answer="Security of AI systems.")])
        messages = []
        class Extractor:
            """A test-only adapter capturing resumed messages; this case does not exercise provider HTTP."""
            def invoke(self, request):
                """Record human guidance and return a typed fixture without spending model credits."""
                messages.extend(request)
                return {"raw": AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                            response_metadata={"stop_reason": "end_turn", "model_name": "fixture"}),
                        "parsed": ExtractionBatch.model_validate(proposed_batch()), "parsing_error": None}
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": planning_tests.TEST_KEY}), \
             patch.object(agentic_workflow, "create_planner", side_effect=AssertionError("Do not replan")), \
             patch("projectclarity.extraction.extract_knowledge_node.create_extractor", return_value=Extractor()):
            session = submit_plan_review(self.cli.output, decision)
        self.assertEqual(session.status, "candidates_saved")
        self.assertEqual(session.usage.requests_attempted, 2)
        self.assertEqual(json.loads(messages[1][1])["human_guidance"][0]["answer"], "Security of AI systems.")

    def test_conflicting_modes_and_missing_budget_fail_before_any_provider_call(self):
        """Opt-in requires a real allowance; plan-only cannot accidentally send source text."""
        for options in [("--extract",), ("--extract", "--max-requests", "2", "--plan-only")]:
            with self.subTest(options=options):
                code, _, _ = self.cli.run_cli(lambda request: self.fail("No request allowed"),
                    "--document", str(self.document), *options, include_urls=False, plan_only=False)
                self.assertEqual(code, 2)
                self.assertEqual(self.cli.requests, [])
                self.assertEqual(list(self.cli.output.glob("*/session.json")), [])

    def test_older_session_remains_download_only(self):
        """Loading a pre-extraction snapshot never silently authorizes new model calls."""
        from tests.research.research_test_fixtures import example_session
        saved = example_session().model_dump(mode="json")
        saved["schema_version"] = "1.5"
        saved["request"].pop("extract_knowledge")
        saved.pop("extraction")
        session = ResearchSession.model_validate(saved)
        self.assertFalse(session.request.extract_knowledge)
        self.assertIsNone(session.extraction)
        self.assertEqual(session.schema_version, "1.5")
