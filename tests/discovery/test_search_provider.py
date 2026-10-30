"""Check the parent search adapter and its subprocess request/response format.

Mocked process or engine responses verify explicit backend selection, normalized
results, stripped credentials, timeout cleanup and failure reporting. Unexpected
engine choices must not fall through to DDGS's automatic selection. No search
request reaches the Internet, and an empty success remains distinct from an
engine error or killed worker.
"""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from projectclarity.discovery.search_provider import SearchProviderError, search_web
from projectclarity.discovery.search_worker import main


class SearchProviderTests(unittest.TestCase):
    """Keep credentials out of workers and preserve timeout/error semantics."""

    def test_worker_uses_named_backend_and_normalizes_provider_results(self):
        """No auto backend fallback or source fetch is hidden inside our adapter.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.text.return_value = [{"href": "https://example.org", "title": "Title", "body": "Lead"}]
        output = io.StringIO()
        with patch("ddgs.DDGS", return_value=worker) as factory, patch("sys.stdin", io.StringIO(json.dumps({
                "query": "test query", "max_results": 2}))), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        factory.assert_called_once_with(timeout=8)
        worker.text.assert_called_once_with("test query", backend="duckduckgo", max_results=2)
        self.assertEqual(json.loads(output.getvalue())[0]["url"], "https://example.org")

    def test_parent_strips_credentials_and_sends_query_via_stdin(self):
        """Search receives only its query/options, not the application's provider credentials.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        process = MagicMock(returncode=0)
        process.communicate.return_value = ('[{"url":"https://example.org"}]', None)
        process.poll.return_value = 0
        with patch("subprocess.Popen", return_value=process) as popen, patch.dict("os.environ", {"ANTHROPIC_API_KEY": "private-test-key"}):
            self.assertEqual(search_web("test query", 2)[0]["url"], "https://example.org")
        self.assertNotIn("ANTHROPIC_API_KEY", popen.call_args.kwargs["env"])
        self.assertNotIn("test query", str(popen.call_args.args))
        self.assertIn("test query", process.communicate.call_args_list[0].kwargs["input"])

    def test_worker_uses_explicit_brave_selection_without_fallback(self):
        """Prove the configured alternate backend reaches DDGS rather than retaining the default.

        Args:
            self: Worker test with a synthetic Brave result list and no network access.

        Returns:
            None. Assert exactly the explicitly requested backend is invoked once.
        """
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.text.return_value = []
        with patch('ddgs.DDGS', return_value=worker), patch('sys.stdin', io.StringIO(json.dumps({
                'query': 'fixture', 'max_results': 2, 'provider': 'brave'}))), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        worker.text.assert_called_once_with('fixture', backend='brave', max_results=2)

    def test_unavailable_engine_cannot_trigger_library_auto_fallback(self):
        """Fail before DDGS can silently replace a configured engine with its automatic set.

        Args:
            self: Worker test simulating a package version without the requested engine.

        Returns:
            None. Assert a visible diagnostic and no constructed search client or network call.
        """
        output = io.StringIO()
        with patch.dict('ddgs.engines.ENGINES', {'text': {}}), patch('ddgs.DDGS') as factory, patch(
                'sys.stdin', io.StringIO(json.dumps({'query': 'fixture', 'max_results': 2}))), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 1)
        factory.assert_not_called()
        self.assertIn('automatic fallback refused', json.loads(output.getvalue())['error']['message'])

    def test_deadline_kills_worker_and_records_timeout(self):
        """A provider thread cannot hold the application indefinitely.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        process = MagicMock(returncode=None)
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired("worker", .25), ("", None)]
        with patch("subprocess.Popen", return_value=process), patch("time.monotonic", side_effect=[0, 0, 21]):
            with self.assertRaisesRegex(SearchProviderError, "search_timeout"):
                search_web("test query", 2)
        process.kill.assert_called_once()

    def test_nonzero_exit_is_failure_not_no_results(self):
        """Engine errors must not masquerade as a successful empty query.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        process = MagicMock(returncode=1)
        process.poll.return_value = 1
        process.communicate.return_value = ("", None)
        with patch("subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(SearchProviderError, "search_provider_failed"):
                search_web("test query", 2)

    def test_worker_reports_rate_limit_details_and_parent_keeps_them_private(self):
        """Retain a concrete provider error without converting failure into an empty success.

        Args:
            self: Test case driving both sides of the search worker protocol without network.

        Returns:
            None. Assert the classified code and bounded message survive the subprocess
            boundary while the exception's public string contains only its safe code.
        """
        from ddgs.exceptions import RatelimitException
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.text.side_effect = RatelimitException('HTTP 429 from engine')
        output = io.StringIO()
        with patch('ddgs.DDGS', return_value=worker), patch('sys.stdin', io.StringIO(json.dumps({
                'query': 'research fixture', 'max_results': 2}))), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 1)
        error = json.loads(output.getvalue())['error']
        self.assertEqual(error['code'], 'search_rate_limited')
        process = MagicMock(returncode=1)
        process.poll.return_value = 1
        process.communicate.return_value = (output.getvalue(), None)
        with patch('subprocess.Popen', return_value=process):
            with self.assertRaises(SearchProviderError) as caught:
                search_web('research fixture', 2)
        self.assertEqual(str(caught.exception), 'search_rate_limited')
        self.assertEqual(caught.exception.diagnostics['type'], 'RatelimitException')
        self.assertIn('429', caught.exception.diagnostics['message'])
