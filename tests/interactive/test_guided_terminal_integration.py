"""Exercise the actual user-facing menus through a real pseudo-terminal.

These tests type into the production CLI, not a separate demo UI. The provider
fixture replaces Anthropic HTTP and denies networking/credential reads; actual
LangGraph interruption, review validation, checkpoints, local file parsing and
saved decisions remain in the path. Source files and outputs are temporary.
Assertions inspect saved evidence as well as terminal output, so a convincing
menu alone cannot count as successful workflow integration.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from projectclarity.run_status_commands import read_status
from tests.interactive.pseudo_terminal_driver import TerminalProcess


class GuidedTerminalIntegrationTests(unittest.TestCase):
    """Check guided decisions and safe exits without touching live credentials or URLs."""

    def setUp(self):
        """Create temporary source/output paths and the import environment for terminal children."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / ".projectclarity/runs"
        self.document = self.directory / "evidence.txt"
        self.document.write_text("Evidence acquired by the real interactive CLI.\n")
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))

    def command(self, *arguments):
        """Use the HTTP-only fixture, then the real projectclarity.__main__ entry point."""
        return [sys.executable, "-m", "tests.interactive.interactive_research_process", str(self.directory), *arguments]

    def terminal(self, *arguments):
        """Keep stdin/stdout as actual terminals; no isatty patch is permitted here."""
        child = TerminalProcess(self.command(*arguments), cwd=self.directory, env=self.env)
        self.addCleanup(child.close)
        return child

    def research(self, *options):
        """Use the existing advanced command, which must now offer review automatically."""
        return self.terminal("research", "--question", "Research AI security", "--document", str(self.document), *options)

    def saved(self):
        """Require one run; a review must not secretly start a replacement research job."""
        files = list(self.output.glob("*/session.json"))
        self.assertEqual(len(files), 1)
        return files[0].parent, json.loads(files[0].read_text())

    def assert_acquired_once(self):
        """Verify real acquisition plus unchanged usage, not just a success message."""
        directory, session = self.saved()
        self.assertEqual(session["status"], "sources_retrieved")
        self.assertEqual(session["usage"]["requests_attempted"], 1)
        self.assertEqual(len(json.loads((self.directory / "provider_calls.json").read_text())), 1)
        self.assertEqual((directory / "documents/S1/text.txt").read_text(), self.document.read_text())
        self.assertEqual(session["visited_nodes"], ["initialize_research", "plan_research", "review_plan", "retrieve_sources"])
        return session

    def test_bare_command_guides_setup_answers_and_fetching_in_one_process(self):
        """The simplest launch needs no command flags, run IDs, or JSON decision file."""
        child = self.terminal()
        child.expect("Choose 1-3:")
        child.send("1\n")
        child.expect("What would you like to research?")
        child.send("Research AI security\n")
        child.expect("URL list file")
        child.send("\n")
        child.expect("Document path")
        child.send("evidence.txt\n\n")
        child.expect("Choose 1-5:")
        directory, session = self.saved()
        self.assertEqual(session["status"], "awaiting_review")
        self.assertFalse((directory / "documents.json").exists())
        child.send("1\n")
        child.expect("Your answer")
        child.send("\n")
        child.expect("Please enter an answer")
        child.send("2024–2026\nYes, both people and organizations.\n")
        child.expect("Ready to continue?")
        child.send("1\n")
        self.assertEqual(child.finish(), 0, child.output)
        session = self.assert_acquired_once()
        self.assertEqual(session["review_decision"]["answers"][0]["answer"], "2024–2026")

    def test_existing_research_command_can_edit_answers_and_return_to_menu(self):
        """Draft edits survive menu navigation but only the final answers are submitted."""
        child = self.research()
        child.expect("Choose 1-5:")
        child.send("1\n2024\nYes\n")
        child.expect("Ready to continue?")
        child.send("2\n1\n2025–2026\n")
        child.expect("Current answer: 2024")
        child.expect("Ready to continue?")
        child.send("3\n")
        child.expect("Choose 1-5:")
        child.send("1\n")
        child.expect("Ready to continue?")
        child.send("1\n")
        self.assertEqual(child.finish(), 0, child.output)
        session = self.assert_acquired_once()
        self.assertEqual(session["review_decision"]["answers"][0]["answer"], "2025–2026")

    def test_question_shortcut_skips_setup_but_keeps_guided_review(self):
        """A pasted command starts one run, then asks only the plan's review questions."""
        question = "Compare 'AI security' research in Canada during 2024–2026."
        # Put another option first and use --question=value: routing must not
        # depend on one particular ordering or split/reinterpret the user's text.
        child = self.terminal("--output-dir", str(self.output), f"--question={question}",
                              "--document", str(self.document))
        child.expect("Choose 1-5:")
        for prompt in ("Choose 1-3:", "What would you like to research?", "URL list file", "Document path (Enter to finish)"):
            self.assertNotIn(prompt, child.output)
        child.send("1\n2024–2026\nInclude people and organizations.\n")
        child.expect("Ready to continue?")
        child.send("1\n")
        self.assertEqual(child.finish(), 0, child.output)
        session = self.assert_acquired_once()
        self.assertEqual(session["request"]["question"], question)
        self.assertEqual(session["review_decision"]["action"], "answer")

    def test_question_shortcut_respects_noninteractive_review_mode(self):
        """Skipping setup must not override an explicit instruction to avoid all prompts."""
        child = self.terminal("--question", "Research AI security", "--document", str(self.document),
                              "--non-interactive")
        self.assertEqual(child.finish(), 2, child.output)
        self.assertNotIn("Choose 1-", child.output)
        directory, session = self.saved()
        self.assertEqual(session["status"], "awaiting_review")
        self.assertEqual(session["usage"]["requests_attempted"], 1)
        self.assertFalse((directory / "documents.json").exists())

    def test_leave_then_select_paused_review_without_copying_an_id(self):
        """A later menu finds the pending checkpoint and continues it with no new model call."""
        first = self.research()
        first.expect("Choose 1-5:")
        first.send("4\n")
        self.assertEqual(first.finish(), 2)
        directory, before = self.saved()
        self.assertFalse((directory / "review_decision.json").exists())
        second = self.terminal()
        second.expect("Choose 1-3:")
        second.send("2\n")
        second.expect("Choose a paused research plan:")
        second.send("1\n")
        second.expect("Choose 1-5:")
        second.send("2\ny\n")
        self.assertEqual(second.finish(), 0, second.output)
        after = self.assert_acquired_once()
        self.assertEqual(before["session_id"], after["session_id"])
        self.assertEqual(after["review_decision"]["action"], "accept_assumptions")

    def test_empty_choices_do_not_approve_and_rejection_fetches_nothing(self):
        """Enter retries a menu and declines an assumption waiver; rejection is explicit."""
        child = self.research()
        child.expect("Choose 1-5:")
        child.send("\n")
        child.expect("Please enter a number")
        child.send("2\n\n")
        child.expect("Proceed without answering these questions?")
        child.expect("Choose 1-5:")
        child.send("3\ny\n")
        self.assertEqual(child.finish(), 0, child.output)
        directory, session = self.saved()
        self.assertEqual(session["status"], "rejected")
        self.assertEqual(session["review_decision"]["action"], "reject")
        self.assertFalse((directory / "documents.json").exists())

    def test_ctrl_c_during_answers_leaves_checkpoint_pending_and_draft_unsaved(self):
        """Interrupting a prompt is not approval, nor cancellation of the saved research run."""
        child = self.research()
        child.expect("Choose 1-5:")
        child.send("1\nDraft answer\n")
        child.expect("Question 2 of 2:")
        child.process.send_signal(signal.SIGINT)
        self.assertEqual(child.finish(), 130, child.output)
        directory, session = self.saved()
        self.assertEqual(session["status"], "awaiting_review")
        self.assertIsNone(session["review_decision"])
        self.assertFalse((directory / "documents.json").exists())
        self.assertTrue(read_status(directory)["waiting_for_input"])
        self.assertIn("unfinished answers were not saved", child.output)

    def test_terminal_eof_leaves_review_pending(self):
        """Ctrl-D must leave a retrievable checkpoint instead of manufacturing a decision."""
        child = self.research()
        child.expect("Choose 1-5:")
        child.send("\x04")
        self.assertEqual(child.finish(), 130, child.output)
        directory, session = self.saved()
        self.assertEqual(session["status"], "awaiting_review")
        self.assertFalse((directory / "review_decision.json").exists())

    def test_noninteractive_flag_disables_prompts_even_in_a_terminal(self):
        """Automation can keep the previous exit-2 review boundary on a real terminal."""
        child = self.research("--non-interactive")
        self.assertEqual(child.finish(), 2, child.output)
        self.assertNotIn("Choose 1-5:", child.output)
        _, session = self.saved()
        self.assertEqual(session["status"], "awaiting_review")

    def test_json_progress_mode_disables_interaction_even_in_a_terminal(self):
        """Machine event output never starts consuming answers from terminal input."""
        child = self.research("--progress-json")
        self.assertEqual(child.finish(), 2, child.output)
        self.assertNotIn("Choose 1-5:", child.output)
        self.assertIn('"event": "review_required"', child.output)

    def test_piped_input_cannot_be_mistaken_for_interactive_approval(self):
        """Even plausible menu keystrokes in stdin do not authorize a non-TTY run."""
        result = subprocess.run(self.command("research", "--question", "Research AI security", "--document", str(self.document)),
            cwd=self.directory, env=self.env, input="2\ny\n", text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Choose 1-5:", result.stdout)
        directory, session = self.saved()
        self.assertEqual(session["status"], "awaiting_review")
        self.assertFalse((directory / "review_decision.json").exists())

    def test_json_review_remains_inspection_only_in_a_terminal(self):
        """The structured contract used by future adapters is unaffected by guided UX."""
        first = self.research("--non-interactive")
        self.assertEqual(first.finish(), 2)
        directory, _ = self.saved()
        second = self.terminal("review", directory.name, "--json")
        self.assertEqual(second.finish(), 0, second.output)
        self.assertEqual(json.loads(second.output)["run_id"], directory.name)
        self.assertFalse((directory / "review_decision.json").exists())

    def test_quit_from_bare_menu_starts_no_work(self):
        """Opening and closing the UI does not call the model or create a research run."""
        child = self.terminal()
        child.expect("Choose 1-3:")
        child.send("3\n")
        self.assertEqual(child.finish(), 0)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.directory / "provider_calls.json").exists())
