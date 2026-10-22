"""Launch the real CLI under a terminal, substituting only Anthropic HTTP.

Tests pass a temporary fixture directory followed by ordinary CLI arguments.
stdin/stdout remain attached to a real pseudo-terminal: isatty(), input(), menu
routing, LangGraph, checkpoints, local parsing and saving are production code.
A fixed provider reply asks two questions; a counter file detects repeated model
calls. Sockets, DNS and credential-file reads are denied. This is not a live model
test or an alternative application workflow.
"""

import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import httpx2


def main():
    """Keep synthetic credentials and provider-call evidence inside the test boundary."""
    directory = Path(sys.argv[1])
    arguments = sys.argv[2:]
    plan = json.loads((Path(__file__).parents[1] / "planning/fixtures/valid_research_plan.json").read_text())
    plan["clarifications"] = ["Which years should be included?", "Include organizations as well as researchers?"]
    counter = directory / "provider_calls.json"
    calls = json.loads(counter.read_text()) if counter.exists() else []

    def reply(request):
        """Record each simulated provider call and return a plan requiring two human answers."""
        calls.append({"method": request.method, "path": request.url.path})
        counter.write_text(json.dumps(calls))
        return httpx2.Response(200, json={
            "id": "msg_terminal_fixture", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5", "content": [{"type": "text", "text": json.dumps(plan)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 200},
        })

    with (
        httpx2.Client(transport=httpx2.MockTransport(reply)) as client,
        patch("langchain_anthropic.chat_models._get_default_httpx_client", return_value=client),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "OFFLINE_TERMINAL_FIXTURE_KEY", "LANGSMITH_TRACING": "false"}),
        patch("projectclarity.research_planner.dotenv_values", side_effect=AssertionError("No credential files allowed")),
        patch("projectclarity.research_planner.getpass.getpass", side_effect=AssertionError("No key prompt allowed")),
        patch("socket.getaddrinfo", side_effect=AssertionError("No DNS allowed")),
        patch("socket.socket.connect", side_effect=AssertionError("No network allowed")),
        patch("sys.argv", ["projectclarity", *arguments]),
    ):
        runpy.run_module("projectclarity", run_name="__main__")


if __name__ == "__main__":
    main()
