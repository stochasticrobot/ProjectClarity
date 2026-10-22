"""Unit tests for planning contracts, application-owned limits, and key resolution.

Purpose:
    Check the planner's Python rules using deliberately good and bad inputs.
    For example, a strategy referring to a nonexistent goal must fail validation,
    and a configured environment key must take precedence over a key file.
    These checks do not need the model to produce an answer.
Responsibilities:
    Reject malformed plans and broken goal links, protect immutable budgets,
    and verify environment/.env/prompt precedence and credential isolation.
    Each file-based key test uses its own temporary path, never the real .env.
Typical use:
    Run ``python -m unittest tests.planning.test_plan_validation_and_credentials -v`` or discover the suite.
    The plan fixture is mutated into invalid cases to exercise validation.
Boundary:
    These checks do not construct the research graph, make provider requests,
    or measure whether the system prompt produces a good research plan.
"""

import json
import contextlib
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from projectclarity.research_schemas import Budget, ResearchPlan
from projectclarity import research_planner as planning


FIXTURE = Path(__file__).parent / "fixtures" / "valid_research_plan.json"


class PlanContractTests(unittest.TestCase):
    """Structural acceptance is not a claim about planning quality or factual truth."""

    def test_old_prompt_revision_is_readable_without_relabeling_saved_state(self):
        """A new prompt must not strand existing review checkpoints or rewrite their provenance."""
        from projectclarity.research_schemas import ResearchSession
        from tests.research.research_test_fixtures import example_session

        for version in ("research-plan-v1", "research-plan-v2", "research-plan-v3"):
            with self.subTest(version=version):
                saved = example_session().model_dump(mode="json")
                saved["prompt_version"] = version
                restored = ResearchSession.model_validate_json(json.dumps(saved))
                self.assertEqual(restored.model_dump(mode="json"), saved)
        self.assertEqual(example_session().prompt_version, "research-plan-v4")

    def test_question_category_is_required_nonblank_text_with_a_length_bound(self):
        """The prompt's category request must produce data, not vanish from the output."""
        for value in (None, "", "   ", 42, ["comparison"], "x" * 161):
            with self.subTest(value=value):
                plan = json.loads(FIXTURE.read_text())
                plan["question_category"] = value
                with self.assertRaises(ValidationError) as caught:
                    ResearchPlan.model_validate(plan)
                self.assertIn(("question_category",), [error["loc"] for error in caught.exception.errors()])
        plan = json.loads(FIXTURE.read_text())
        del plan["question_category"]
        with self.assertRaises(ValidationError):
            ResearchPlan.model_validate(plan)

    def test_question_category_is_open_ended_and_survives_json_round_trip(self):
        """Useful new labels do not require a fixed taxonomy; outer whitespace is trimmed."""
        plan = json.loads(FIXTURE.read_text())
        plan["question_category"] = "  Comparison of research methods  "
        accepted = ResearchPlan.model_validate(plan)
        restored = ResearchPlan.model_validate_json(accepted.model_dump_json())
        self.assertEqual(restored.question_category, "Comparison of research methods")

    def test_duplicate_unknown_and_uncovered_goals_are_rejected(self):
        """A strategy must cover each unique goal and cannot refer to invented IDs."""
        # Reload each time: the malformed variants must not contaminate others.
        for problem in ["duplicate", "unknown", "uncovered"]:
            with self.subTest(problem=problem):
                plan = json.loads(FIXTURE.read_text())
                if problem == "duplicate":
                    plan["goals"][1]["goal_id"] = "G1"
                elif problem == "unknown":
                    plan["strategy"][0]["goal_ids"].append("G99")
                else:
                    plan["strategy"][0]["goal_ids"] = ["G1"]
                with self.assertRaises(ValidationError):
                    ResearchPlan.model_validate(plan)

    def test_missing_blank_and_extra_fields_are_rejected(self):
        """Valid JSON is insufficient; all contract fields and boundaries matter."""
        for problem in ["missing", "blank", "budget_override"]:
            with self.subTest(problem=problem):
                plan = json.loads(FIXTURE.read_text())
                if problem == "missing":
                    del plan["objective"]
                elif problem == "blank":
                    plan["objective"] = "   "
                else:
                    # An LLM must not smuggle new execution limits into its plan.
                    plan["budget"] = {"max_requests": 1000}
                with self.assertRaises(ValidationError):
                    ResearchPlan.model_validate(plan)

    def test_budget_limits_are_bounded_and_immutable(self):
        """Budget changes require a new explicit configuration, not mutation by nodes."""
        for fields in [{"max_requests": -1}, {"max_requests": 31}, {"max_output_tokens": 0}, {"max_output_tokens": 8193}]:
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    Budget(**fields)
        budget = Budget()
        with self.assertRaises(ValidationError):
            budget.max_requests = 10


class CredentialInputTests(unittest.TestCase):
    """Test the application's key reader separately from the older smoke helper."""

    def setUp(self):
        # Never read the developer's real .env, even after they populate it.
        """Redirect credential-file lookup to a disposable path, never the developer's real .env."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env_file = Path(self.temporary.name) / ".env"
        env_path = patch.object(planning, "PROJECT_ENV_FILE", self.env_file)
        env_path.start()
        self.addCleanup(env_path.stop)

    def test_environment_key_does_not_open_a_prompt(self):
        """An already-configured credential stays out of interactive input and output."""
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "synthetic-test-key"}),
            patch.object(planning.getpass, "getpass") as prompt,
            patch.object(planning, "dotenv_values") as read_file,
        ):
            self.assertEqual(planning.read_api_key(), "synthetic-test-key")
            prompt.assert_not_called()
            read_file.assert_not_called()

    def test_project_file_key_is_loaded_without_prompt_or_environment_mutation(self):
        """Local persistence avoids repeated entry but does not export the file's data."""
        self.env_file.write_text('ANTHROPIC_API_KEY="synthetic-file-key"\nUNRELATED_TEST_SETTING=ignored\n')
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}),
            patch.object(planning.getpass, "getpass") as prompt,
        ):
            original_environment = dict(os.environ)
            self.assertEqual(planning.read_api_key(), "synthetic-file-key")
            self.assertEqual(dict(os.environ), original_environment)
            prompt.assert_not_called()

    def test_file_values_are_not_interpolated_or_executed(self):
        """A .env is configuration data, not shell code or an environment template."""
        self.env_file.write_text("ANTHROPIC_API_KEY='synthetic-${OTHER_TEST_VALUE}-$(not-a-command)'\n")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "OTHER_TEST_VALUE": "expanded"}):
            self.assertEqual(planning.read_api_key(), "synthetic-${OTHER_TEST_VALUE}-$(not-a-command)")

    def test_unrelated_working_directory_cannot_supply_a_key(self):
        """Only the explicit project file is used, never a different cwd's .env."""
        unrelated = Path(self.temporary.name) / "unrelated"
        unrelated.mkdir()
        (unrelated / ".env").write_text("ANTHROPIC_API_KEY=wrong-project-key\n")
        self.env_file.write_text("ANTHROPIC_API_KEY=correct-project-key\n")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}), contextlib.chdir(unrelated):
            self.assertEqual(planning.read_api_key(), "correct-project-key")

    def test_unreadable_file_has_a_controlled_diagnostic(self):
        """Configuration errors must not include arbitrary file/exception contents."""
        self.env_file.write_text("ANTHROPIC_API_KEY=synthetic-private-value\n")
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}),
            patch.object(planning, "dotenv_values", side_effect=OSError("synthetic-private-value")),
        ):
            with self.assertRaises(planning.ResearchError) as caught:
                planning.read_api_key()
            self.assertIn("Cannot read the project .env", str(caught.exception))
            self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_noninteractive_input_is_refused_without_environment_key(self):
        """A missing key in noninteractive mode must fail before asking for a secret."""
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}),
            patch.object(planning.sys.stdin, "isatty", return_value=False),
            patch.object(planning.getpass, "getpass") as prompt,
        ):
            with self.assertRaisesRegex(planning.ResearchError, "interactive terminal"):
                planning.read_api_key()
            prompt.assert_not_called()

    def test_visible_input_fallback_is_stopped_before_reading_a_key(self):
        """A terminal that cannot hide input must not fall back to echoing secrets."""
        def cannot_hide_input(_prompt):
            """Emit the hidden-input warning and fail if the application allows visible fallback."""
            warnings.warn("Cannot hide input", planning.getpass.GetPassWarning)
            self.fail("The warning should have stopped visible input")

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}),
            patch.object(planning.sys.stdin, "isatty", return_value=True),
            patch.object(planning.getpass, "getpass", side_effect=cannot_hide_input),
        ):
            with self.assertRaisesRegex(planning.ResearchError, "cannot hide input"):
                planning.read_api_key()
