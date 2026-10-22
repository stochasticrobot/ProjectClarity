"""Check that rejected model answers explain their failure without leaking data.

For example, a missing category should name that field, while a model-invented
field containing a secret must be shown only as <unknown-field>. Integration
checks run the real CLI, graph, LangChain parser, and file writes against synthetic
provider HTTP. Small unit checks cover unusual exception chains and empty parser
results. No real key, source fetch, or paid model call is used.

Run ``python -m unittest tests.planning.test_planning_failure_diagnostics -v``.
These tests verify diagnostics and rejection, not the cause of a historical live
failure whose original validation details were discarded.
"""

import json
import unittest

from projectclarity.planning_failure_diagnostics import describe_planning_failure, safe_field_path
from tests.planning import test_planning_cli_integration as planning_tests


class PlanningFailureIntegrationTests(unittest.TestCase):
    """Exercise real SDK exceptions and ensure invalid answers cannot start retrieval."""

    def run_rejected_reply(self, *, mutate=None, text=None):
        """Return safe diagnostics from an isolated real CLI run with one fake reply."""
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        try:
            if mutate:
                mutate(cli.plan)
            code, stdout, stderr = cli.run_cli(lambda request: cli.response(text=text), plan_only=False)
            self.assertEqual(code, 1, stderr)
            directory, saved = cli.saved_session()
            diagnostic = json.loads((directory / "planning_failure.json").read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertIsNone(saved["plan"])
            self.assertEqual(saved["visited_nodes"], ["initialize_research", "plan_research"])
            self.assertEqual(saved["usage"]["requests_attempted"], 1)
            self.assertEqual(saved["usage"]["stop_reason"], "end_turn")
            self.assertEqual(len(cli.requests), 1)
            for name in ("plan.json", "plan.md", "documents.json", "review_request.json"):
                self.assertFalse((directory / name).exists(), name)
            events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
            finished = next(item for item in events if item["event"] == "planning_finished")
            self.assertEqual(finished["details"]["error_code"], diagnostic["error_code"])
            self.assertNotIn("issues", finished["details"])
            # Inspect every saved artifact, including native checkpoints, for
            # values that appeared only in the synthetic provider's bad answer.
            artifacts = b"\n".join(path.read_bytes() for path in cli.output.rglob("*") if path.is_file())
            return diagnostic, stdout + stderr, artifacts
        finally:
            cli.doCleanups()

    def test_nested_field_errors_are_explained_without_values_or_unknown_names(self):
        """Retain schema locations, but redact rejected values and invented field names."""
        secret = planning_tests.TEST_KEY

        def invalid(plan):
            """Inject missing/wrong/private fields to test bounded diagnostics without leaking their values."""
            del plan["question_category"]
            plan["goals"][0]["evidence_required"][0] = {"private": secret}
            plan[secret] = "PRIVATE_MODEL_TEXT_NOT_FOR_LOGS"

        diagnostic, terminal, artifacts = self.run_rejected_reply(mutate=invalid)
        self.assertEqual(diagnostic["error_code"], "plan_schema_invalid")
        issues = {(item["path"], item["code"]) for item in diagnostic["issues"]}
        self.assertIn(("question_category", "missing"), issues)
        self.assertIn(("goals[0].evidence_required[0]", "string_type"), issues)
        self.assertIn(("<unknown-field>", "extra_forbidden"), issues)
        self.assertIn("question_category: Required field is missing.", terminal)
        for private in (secret, "PRIVATE_MODEL_TEXT_NOT_FOR_LOGS"):
            self.assertNotIn(private, terminal)
            self.assertNotIn(private.encode(), artifacts)

    def test_goal_link_failures_have_distinct_actionable_codes(self):
        """Separate duplicate IDs, nonexistent goals, and goals with no strategy coverage."""
        for expected in ("duplicate_goal_ids", "strategy_unknown_goals", "strategy_uncovered_goals"):
            with self.subTest(rule=expected):
                def invalid(plan):
                    """Create the selected duplicate, unknown or uncovered goal-link error in the fixture."""
                    if expected == "duplicate_goal_ids":
                        plan["goals"][1]["goal_id"] = "G1"
                    elif expected == "strategy_unknown_goals":
                        plan["strategy"][0]["goal_ids"].append("G99")
                    else:
                        plan["strategy"][0]["goal_ids"] = ["G1"]
                diagnostic, terminal, _ = self.run_rejected_reply(mutate=invalid)
                self.assertEqual(diagnostic["issues"][0]["code"], expected)
                self.assertIn(diagnostic["issues"][0]["message"], terminal)

    def test_invalid_json_is_distinguished_without_echoing_the_reply(self):
        """The SDK's JSON parser cause is enough; its raw llm_output must stay private."""
        diagnostic, terminal, artifacts = self.run_rejected_reply(
            text='{"objective": PRIVATE_INVALID_JSON}')
        self.assertEqual(diagnostic["error_code"], "plan_json_invalid")
        self.assertEqual(diagnostic["issues"], [])
        self.assertIn("could not be parsed as JSON", terminal)
        self.assertNotIn("PRIVATE_INVALID_JSON", terminal)
        self.assertNotIn(b"PRIVATE_INVALID_JSON", artifacts)

    def test_many_invalid_fields_produce_a_bounded_artifact(self):
        """A malformed answer cannot flood diagnostic files with repeated private fields."""
        def invalid(plan):
            """Add twenty invalid fields to verify the diagnostic issue limit and private-name filtering."""
            plan.update({f"PRIVATE_FIELD_{number}": "PRIVATE_VALUE" for number in range(20)})
        diagnostic, terminal, artifacts = self.run_rejected_reply(mutate=invalid)
        self.assertEqual(len(diagnostic["issues"]), 8)
        self.assertTrue(diagnostic["issues_truncated"])
        self.assertIn("planning_failure.json", terminal)
        self.assertNotIn(b"PRIVATE_FIELD", artifacts)
        self.assertNotIn(b"PRIVATE_VALUE", artifacts)


class PlanningDiagnosticBoundaryTests(unittest.TestCase):
    """Use predictable fallbacks when no structured validation detail is available."""

    def test_missing_result_and_unknown_parser_failure_are_not_conflated(self):
        """An empty parser result differs from an exception with no safe field details."""
        self.assertEqual(describe_planning_failure(None).error_code, "plan_result_missing")
        failure = describe_planning_failure(RuntimeError("PRIVATE_EXCEPTION_MESSAGE"))
        self.assertEqual(failure.error_code, "plan_parse_failed")
        self.assertNotIn("PRIVATE_EXCEPTION_MESSAGE", failure.model_dump_json() + failure.explanation())

    def test_cyclic_exception_chain_terminates_without_printing_exception_text(self):
        """A broken wrapper cannot loop forever while the application diagnoses failure."""
        first, second = RuntimeError("PRIVATE_FIRST"), RuntimeError("PRIVATE_SECOND")
        first.__cause__, second.__cause__ = second, first
        failure = describe_planning_failure(first)
        self.assertEqual(failure.error_code, "plan_parse_failed")
        self.assertNotIn("PRIVATE", failure.model_dump_json())

    def test_field_paths_are_bounded_and_hide_unknown_components(self):
        """Only known schema names and bounded list positions may be displayed."""
        self.assertEqual(safe_field_path(()), "plan")
        self.assertEqual(safe_field_path(("goals", 0, "goal_id")), "goals[0].goal_id")
        path = safe_field_path(tuple(["PRIVATE_NAME", -1, 10001] * 100))
        self.assertNotIn("PRIVATE", path)
        self.assertNotIn("10001", path)
        self.assertLessEqual(len(path), 240)
