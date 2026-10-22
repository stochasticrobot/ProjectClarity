"""Explicit live connection check: one real model call through LangGraph.

Purpose:
    Answer a narrow setup question: can this key get a text reply from Anthropic
    through LangGraph? Running it sends one real request and prints the reply and
    usage. It helps distinguish a connection/account problem from a problem in
    the research planner; it does not ask the model to do research.
Responsibilities:
    Read a key through a hidden prompt, send one bounded request, require a
    completed text answer, and report model/usage metadata without saving the key.
Typical use:
    Run ``python -m tests.llm_connection.live_connection_check`` from the repository root.
    This standalone check retains its hidden prompt rather than the application's
    environment/.env lookup. It can spend credits and must be invoked explicitly.
Boundary:
    The filename is deliberately not test_*.py, so offline discovery does not
    execute it. Importing only defines code. Success establishes connection
    wiring, not research planning, extraction quality, or the full researcher.
"""

import argparse
import getpass
import json
import sys
import warnings
from typing import TypedDict

from langchain_anthropic import ChatAnthropic
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context


DEFAULT_MODEL = "claude-haiku-5-5"
MAX_OUTPUT_TOKENS = 512
# A fixed, non-sensitive prompt keeps this connection check small and repeatable.
PROMPT = "Reply with one short sentence confirming that you received this message."


class SmokeTestError(RuntimeError):
    """A controlled, credential-free diagnostic safe to print."""


class SmokeState(TypedDict, total=False):
    """Data passed between graph nodes; the answer fields arrive after the call."""

    # Credentials deliberately never enter graph state.
    prompt: str
    text: str
    response_model: str
    stop_reason: str
    usage: dict


def read_key() -> str:
    """Read a key without echoing it; refuse getpass's visible-input fallback."""
    # Piped input cannot provide the interactive, hidden prompt this check needs.
    if not sys.stdin.isatty():
        raise SmokeTestError("Run this command in an interactive terminal for hidden key entry.")
    try:
        with warnings.catch_warnings():
            # getpass normally warns and falls back to visible input. Raising on
            # that warning stops it before a developer could expose their key.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Anthropic API key (hidden; not saved): ").strip()
    except getpass.GetPassWarning:
        raise SmokeTestError("This terminal cannot hide input; no key was read.") from None
    if not key:
        raise SmokeTestError("No API key supplied.")
    return key


def run_smoke(api_key: str, model_id: str = DEFAULT_MODEL) -> dict:
    """Execute START -> ask_haiku -> END with no retry, fallback, or tools."""
    # Keep provider configuration explicit: one bounded request, no hidden retry,
    # and a known endpoint rather than an environment-selected proxy URL.
    model = ChatAnthropic(
        model=model_id,
        api_key=api_key,
        base_url="https://api.anthropic.com",
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=30.0,
        max_retries=0,
        output_config={"effort": "low"},
        # Omit temperature/top_p/top_k: Haiku 5.5 restricts these parameters.
    )

    def ask_haiku(state: SmokeState) -> SmokeState:
        """Invoke the provider and translate its response into our graph state."""
        response = model.invoke(state["prompt"])
        content = response.content
        if isinstance(content, str):
            text = content
        else:
            # Thinking blocks are not the answer and must not be printed.
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        return {
            "text": text,
            "response_model": response.response_metadata.get("model_name", "unknown"),
            "stop_reason": response.response_metadata.get("stop_reason", "unknown"),
            "usage": response.usage_metadata or {},
        }

    # This is the actual one-node LangGraph execution path. Offline integration
    # tests exercise it too, substituting only the provider's HTTP transport.
    builder = StateGraph(SmokeState)
    builder.add_node("ask_haiku", ask_haiku)
    builder.add_edge(START, "ask_haiku")
    builder.add_edge("ask_haiku", END)
    graph = builder.compile()

    # Do not export this standalone check to LangSmith, even if enabled in the
    # environment. The application has local monitoring; neither this smoke
    # check nor the application enables remote tracing implicitly.
    with tracing_context(enabled=False):
        state = graph.invoke({"prompt": PROMPT}, config={"recursion_limit": 3})

    # HTTP success alone is insufficient: a blank or token-truncated answer must
    # not produce a misleading "connection test passed" result.
    if not state["text"].strip() or state["stop_reason"] != "end_turn":
        raise SmokeTestError(
            "The API responded, but did not produce a completed text answer. "
            "The smoke test has not passed; no automatic retry was made."
        )
    return {"requested_model": model_id, **{k: v for k, v in state.items() if k != "prompt"}}


def main() -> int:
    """Provide the interactive CLI and return a shell-friendly success/failure code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Explicit model ID; no automatic fallback")
    args = parser.parse_args()
    print(f"One LangGraph call to {args.model}; output cap {MAX_OUTPUT_TOKENS}; retries 0.")
    try:
        result = run_smoke(read_key(), args.model)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
    except SmokeTestError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not dump provider exception bodies, headers, or a credential-bearing
        # traceback. The category/status is enough for the next diagnostic step.
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if status is not None else ""
        print(f"Call failed: {type(exc).__name__}{detail}. No retry was made.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print("LangGraph + Anthropic connection test passed.")
    return 0


if __name__ == "__main__":
    # Importing this module in offline tests must never prompt or invoke a model.
    raise SystemExit(main())
