"""Check the exit codes and error messages that scripts and operators depend on.

For example, a cancelled run must return 130, not look like a completed job.
The graph is replaced with a returned session or an injected exception so a
failure here points to CLI handling, not Anthropic, retrieval, or storage. The
separate integration tests still check that the actual graph is connected.
"""

import contextlib
import io
import unittest
from unittest.mock import patch

from projectclarity.command_line import main
from projectclarity.research_planner import ResearchError
from tests.research.research_test_fixtures import example_session


class CommandLineOutcomeTests(unittest.TestCase):
    """Replace graph execution to check exit codes without provider calls or run writes.

    Fixture construction reads the handwritten plan; result display may check
    for a saved graph file. These are not filesystem-isolation or acquisition tests.
    """

    def invoke(self, *extra_args):
        """Capture exactly what a script receives from this command invocation."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["research", "--question", "Find Canadian AI research", *extra_args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_session_outcomes_map_to_distinct_exit_codes(self):
        """Success, clarification, failure, and cancellation remain machine-distinguishable."""
        cases = [
            ("plan_ready", True, True, 0), ("needs_clarification", True, True, 0),
            ("needs_clarification", False, True, 2), ("sources_retrieved", False, True, 0),
            ("retrieval_partial", False, True, 1), ("retrieval_failed", False, True, 1),
            ("failed", True, False, 1), ("cancelled", True, False, 130),
            ("cancelled", False, True, 130),
        ]
        for status, plan_only, with_plan, expected in cases:
            with self.subTest(status=status, plan_only=plan_only, with_plan=with_plan):
                session = example_session(with_plan=with_plan, plan_only=plan_only)
                session.status = status
                with patch("projectclarity.command_line.run_research", return_value=session):
                    code, _, _ = self.invoke(*(["--plan-only"] if plan_only else []))
                self.assertEqual(code, expected)

    def test_execution_errors_are_safe_and_have_failure_exit_codes(self):
        """Infrastructure exceptions reveal their type, not arbitrary private payloads."""
        cases = [(OSError("PRIVATE_PAYLOAD"), 1, "OSError"),
                 (KeyboardInterrupt(), 130, "Cancelled"),
                 (EOFError(), 130, "Cancelled"),
                 (ResearchError("Invalid source list."), 2, "Invalid source list.")]
        for error, expected, diagnostic in cases:
            with self.subTest(error=type(error).__name__):
                with patch("projectclarity.command_line.run_research", side_effect=error):
                    code, stdout, stderr = self.invoke()
                self.assertEqual(code, expected)
                self.assertIn(diagnostic, stderr)
                self.assertNotIn("PRIVATE_PAYLOAD", stdout + stderr)

    def test_invalid_cli_input_stops_before_starting_work(self):
        """A bad configuration is diagnosed locally before a worker or model starts."""
        with patch("projectclarity.command_line.run_research") as run:
            code, _, stderr = self.invoke("--max-requests", "-1")
        self.assertEqual(code, 2)
        self.assertIn("max_requests", stderr)
        run.assert_not_called()

    def test_shortcut_help_and_invalid_options_never_start_research(self):
        """The shortcut preserves argparse validation and a read-only root help command."""
        cases = [(["--help"], 0), (["--urls", "source_urls.txt"], 2),
                 (["--question", ""], 2), (["--question", "Q", "--unknown-option"], 2),
                 (["--question", "Q", "--max-requests", "-1"], 2)]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("projectclarity.command_line.run_research") as run, \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    try:
                        code = main(arguments)
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(code, expected)
                run.assert_not_called()
                if arguments == ["--help"]:
                    self.assertIn("Quick start:", stdout.getvalue())
                    self.assertIn("--question", stdout.getvalue())
