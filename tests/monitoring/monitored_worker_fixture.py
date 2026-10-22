"""Run the real research CLI in a child process with a deliberately slow source.

The monitoring integration tests start this module, then run normal status/cancel
commands in other processes. Only Anthropic HTTP, source HTTP, and DNS are faked;
the CLI, LangGraph, parsers, files, locks, progress events, and cancellation checks
are production code. Source one is quick, source two downloads slowly, and source
three proves that a stop request prevents later work. This is not a live smoke
test or evidence about provider/research quality.
"""

import argparse
import json
import runpy
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx2

from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns


def main():
    """Accept a temporary output folder; never use a developer's real credential file."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    sources = args.output_dir.parent / "fixture_sources.txt"
    sources.write_text("https://example.org/first?token=QUERY_SECRET\nhttps://example.org/slow\nhttps://example.org/last\n")
    plan = json.loads((Path(__file__).parents[1] / "planning/fixtures/valid_research_plan.json").read_text())
    plan["clarifications"] = []
    plan["strategy"][0]["source_ids"] = ["S1", "S2", "S3"]
    http = FakeHTTP({
        ("example.org", "/robots.txt"): (200, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /\n"),
        ("example.org", "/first?token=QUERY_SECRET"): (200, {"Content-Type": "text/plain"}, b"DO_NOT_LOG_DOCUMENT_BODY\n"),
        ("example.org", "/slow"): (200, {"Content-Type": "text/plain"}, b"slow source notes\n" * 20000),
        ("example.org", "/last"): (200, {"Content-Type": "text/plain"}, b"Last source\n"),
    })

    def source_pool(host, **options):
        """Wrap the fake connection pool so one chosen document downloads slowly enough to cancel."""
        pool = http.pool(host, **options)
        original_request = pool.request

        def request(method, path, **kwargs):
            """Use normal fixture routing, adding a slow read only for the second source."""
            response = original_request(method, path, **kwargs)
            if path == "/slow":
                original_read = response.read

                def slow_read(amount, **read_options):
                    """Deliver small delayed chunks so the production worker can observe a stop between reads."""
                    time.sleep(0.05)
                    return original_read(min(amount, 1024), **read_options)

                response.read = slow_read
            return response

        pool.request = request
        return pool

    def model_reply(request):
        """Return a no-clarification plan so worker-control tests reach document acquisition."""
        return httpx2.Response(200, json={
            "id": "msg_monitor_fixture", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5", "content": [{"type": "text", "text": json.dumps(plan)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 200},
        })

    with (
        httpx2.Client(transport=httpx2.MockTransport(model_reply)) as client,
        patch("langchain_anthropic.chat_models._get_default_httpx_client", return_value=client),
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "SYNTHETIC_KEY_DO_NOT_LOG"}),
        patch("projectclarity.research_planner.PROJECT_ENV_FILE", args.output_dir.parent / "no-real-env"),
        patch("projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool", side_effect=source_pool),
        patch("socket.getaddrinfo", side_effect=public_dns),
        patch("socket.socket.connect", side_effect=AssertionError("No network allowed")),
        patch("sys.argv", ["projectclarity", "research", "--question", "Inspect supplied research fixtures",
                           "--urls", str(sources), "--output-dir", str(args.output_dir)]),
    ):
        runpy.run_module("projectclarity", run_name="__main__")


if __name__ == "__main__":
    main()
