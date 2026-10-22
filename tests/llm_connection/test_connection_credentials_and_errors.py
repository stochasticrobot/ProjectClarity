"""Isolated unit checks for the standalone connection smoke check's input and CLI.

Purpose:
    Check small parts of the connection check without contacting Anthropic.
    For example, refuse key entry if the terminal would display the secret,
    and ensure an error message cannot print a key supplied by a test.
Responsibilities:
    Mock terminal/input conditions and exceptions, then verify that invalid key
    entry is refused and error output cannot reveal a synthetic credential.
Typical use:
    Run ``python -m unittest tests.llm_connection.test_connection_credentials_and_errors -v`` or discover all
    tests. No provider calls, real credentials, or network access are required.
Boundary:
    This file tests the smoke helper, not research_planner.py's environment/.env reader;
    the latter has separate tests under tests/planning/.
"""

import contextlib
import io
import unittest
import warnings
from unittest.mock import patch

from tests.llm_connection import live_connection_check as smoke


class KeyInputTests(unittest.TestCase):
    """Exercise input boundaries without reading a developer's real terminal."""

    def test_noninteractive_terminal_is_rejected_before_prompting(self):
        """Piped or redirected input must fail before requesting a secret."""
        # Arrange a non-terminal input stream; no real prompt should be reached.
        with (
            patch.object(smoke.sys.stdin, "isatty", return_value=False),
            patch.object(smoke.getpass, "getpass") as prompt,
        ):
            # Assert both the useful diagnostic and the absence of credential IO.
            with self.assertRaisesRegex(smoke.SmokeTestError, "interactive terminal"):
                smoke.read_key()
            prompt.assert_not_called()

    def test_hidden_input_warning_stops_visible_fallback(self):
        """Even a terminal can lack hidden input; its warning must stop entry."""
        def cannot_hide_input(_prompt):
            # Reproduce getpass's warning path, not just a non-terminal stream.
            """Simulate getpass warning before visible input so the test can verify that entry is stopped."""
            warnings.warn("Cannot hide terminal input", smoke.getpass.GetPassWarning)
            self.fail("Visible-input fallback should have been prevented")

        with (
            patch.object(smoke.sys.stdin, "isatty", return_value=True),
            patch.object(smoke.getpass, "getpass", side_effect=cannot_hide_input),
        ):
            with self.assertRaisesRegex(smoke.SmokeTestError, "cannot hide input"):
                smoke.read_key()

    def test_blank_key_is_rejected(self):
        """An empty entry must fail locally instead of reaching the provider."""
        with (
            patch.object(smoke.sys.stdin, "isatty", return_value=True),
            patch.object(smoke.getpass, "getpass", return_value="   "),
        ):
            with self.assertRaisesRegex(smoke.SmokeTestError, "No API key supplied"):
                smoke.read_key()


class CommandLineTests(unittest.TestCase):
    """Check CLI diagnostics independently of graph and provider behavior."""

    def test_unexpected_error_does_not_expose_credentials(self):
        """An exception containing a secret must print its type, not its body."""
        # This is a synthetic credential. Deliberately place it inside the error
        # to catch regressions that print exception bodies or full tracebacks.
        secret = "sk-ant-offline-test-only"
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(smoke, "read_key", return_value=secret),
            patch.object(smoke, "run_smoke", side_effect=RuntimeError(secret)),
            patch.object(smoke.sys, "argv", ["llm-connection-smoke"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = smoke.main()

        # A failing exit code is useful to scripts; neither output stream may
        # reveal the credential that was embedded in the simulated exception.
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("passed", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
