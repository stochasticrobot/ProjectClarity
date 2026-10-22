"""Offline integration checks for the standalone connection smoke workflow.

Purpose:
    Check that the connection-check command sends the intended request and
    handles the reply correctly. For example, a pretend reply that stopped at
    its token limit must be rejected rather than reported as a successful check.
    The reply comes from a local HTTP substitute, so no credits are spent.
Responsibilities:
    Supply in-memory HTTP fixtures and inspect serialized requests, propagated
    metadata, retry behavior, and rejection of incomplete responses. The graph
    and SDK run normally; only the HTTP client/credential input are substituted.
Typical use:
    Run ``python -m unittest tests.llm_connection.test_connection_graph_integration -v`` or use
    ordinary test discovery. No real key or credits are needed.
Boundary:
    These are integration tests, not unit tests or evidence of live account
    access. They do not exercise the application's research-planning graph.
"""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx2

from tests.llm_connection import live_connection_check as smoke


# Never substitute a real credential in fixtures, assertions, or test output.
TEST_KEY = "sk-ant-offline-test-only"


def message_response(text="Offline fixture response.", stop_reason="end_turn"):
    """Build a small provider-shaped response for the real SDK to deserialize."""
    return httpx2.Response(200, json={
        "id": "msg_offline_fixture",
        "type": "message",
        "role": "assistant",
        "model": smoke.DEFAULT_MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 5},
    })


class GraphProviderIntegrationTests(unittest.TestCase):
    """Check connection behavior using deterministic HTTP responses, not a model."""

    def test_real_graph_reaches_sdk_with_bounded_request(self):
        """A graph run must issue one bounded request and preserve the answer/usage."""
        requests = []

        def respond(request):
            # Inspect the serialized wire request, not just constructor settings.
            # The credential belongs in authentication headers, never the prompt.
            """Inspect the serialized provider request and return a bounded in-memory success reply."""
            requests.append(request)
            self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
            self.assertEqual(request.headers["x-api-key"], TEST_KEY)
            body = json.loads(request.content)
            self.assertEqual(body["model"], smoke.DEFAULT_MODEL)
            self.assertEqual(body["max_tokens"], 512)
            self.assertNotIn("temperature", body)
            self.assertNotIn("tools", body)
            self.assertNotIn(TEST_KEY, request.content.decode())
            return message_response()

        # Replace LangChain's HTTP client at the transport boundary. MockTransport
        # handles the request entirely in memory; no Anthropic request is sent.
        # This private client factory belongs to our pinned dependency version;
        # revisit the patch when upgrading langchain-anthropic.
        with (
            httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
            patch(
                "langchain_anthropic.chat_models._get_default_httpx_client",
                return_value=client,
            ),
        ):
            result = smoke.run_smoke(TEST_KEY)

        # The result must retain useful provider metadata while omitting secrets.
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["text"], "Offline fixture response.")
        self.assertEqual(result["requested_model"], smoke.DEFAULT_MODEL)
        self.assertEqual(result["response_model"], smoke.DEFAULT_MODEL)
        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertEqual(result["usage"]["total_tokens"], 17)
        self.assertNotIn(TEST_KEY, json.dumps(result))

    def test_throttling_is_not_retried_or_reported_as_success(self):
        """A simulated HTTP 429 must fail once, without retries or secret leakage."""
        requests = []

        def respond(request):
            # An HTTP error response exercises actual SDK error conversion and
            # retry behavior; raising a fake Python exception would not do that.
            """Count actual transport attempts and return HTTP 429 to exercise SDK retry/error handling."""
            requests.append(request)
            return httpx2.Response(429, json={
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "fixture"},
            })

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
            patch(
                "langchain_anthropic.chat_models._get_default_httpx_client",
                return_value=client,
            ),
            patch.object(smoke, "read_key", return_value=TEST_KEY),
            patch.object(smoke.sys, "argv", ["llm-connection-smoke"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = smoke.main()

        # Count wire attempts as well as checking the CLI result: a hidden retry
        # could otherwise pass while violating this smoke test's request budget.
        self.assertEqual(code, 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("429", stderr.getvalue())
        self.assertNotIn("passed", stdout.getvalue())
        self.assertNotIn(TEST_KEY, stdout.getvalue() + stderr.getvalue())

    def test_incomplete_answers_are_not_reported_as_success(self):
        """HTTP 200 with blank or token-truncated text must still fail the check."""
        # These are distinct failure modes: no usable answer, or an answer that
        # was cut short. A successful HTTP status does not establish completion.
        for text, stop_reason in [("", "end_turn"), ("Partial answer", "max_tokens")]:
            with self.subTest(text=text, stop_reason=stop_reason):
                requests = []

                def respond(request):
                    """Count requests and return the current blank/truncated response case without networking."""
                    requests.append(request)
                    return message_response(text=text, stop_reason=stop_reason)

                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
                    patch(
                        "langchain_anthropic.chat_models._get_default_httpx_client",
                        return_value=client,
                    ),
                    patch.object(smoke, "read_key", return_value=TEST_KEY),
                    patch.object(smoke.sys, "argv", ["llm-connection-smoke"]),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = smoke.main()

                self.assertEqual(code, 1)
                self.assertEqual(len(requests), 1)
                self.assertIn("did not produce a completed text answer", stderr.getvalue())
                self.assertNotIn("passed", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
