"""Run the production CLI in a fresh interpreter with external calls forbidden.

Review integration tests launch this module instead of trusting a future code
change not to contact Anthropic. It delegates to projectclarity.__main__ after
denying sockets, credential-file reads, hidden key prompts, and planner creation.
The graph, checkpoints, review commands, parsers, and storage remain real.
This fixture deliberately covers local-document runs without extraction enabled.
Production review continuation may fetch approved URLs and, with --extract,
make new extraction calls; it does not repeat the original planning call.
This helper is test-only; users run `python -m projectclarity` directly.
"""

import runpy
from unittest.mock import patch


def main():
    """Fail immediately if a review command attempts unrelated external work."""
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Review attempted networking")),
        patch("socket.getaddrinfo", side_effect=AssertionError("Review attempted DNS")),
        patch("projectclarity.research_planner.dotenv_values", side_effect=AssertionError("Review read credentials")),
        patch("projectclarity.research_planner.getpass.getpass", side_effect=AssertionError("Review prompted for a key")),
        patch("projectclarity.agentic_workflow.create_planner", side_effect=AssertionError("Review reran the planner")),
    ):
        runpy.run_module("projectclarity", run_name="__main__")


if __name__ == "__main__":
    main()
