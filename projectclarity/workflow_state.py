"""Declare the shared data passed between LangGraph research steps.

ResearchState names the request and session entries in the workflow dictionary.
The request describes what the user asked to run; the session accumulates the plan,
sources, findings, usage and execution status. agentic_workflow builds the graph
with this state declaration and converts values for saved checkpoints. This file
contains type declarations only, not step execution or storage code.
"""
from typing import TypedDict


class ResearchState(TypedDict, total=False):
    """Information carried between nodes: a request and session, never an API key."""

    # Checkpoints save these entries as plain dictionaries. Each step validates
    # them into the expected request/session models before using them; the saved
    # data does not instruct the checkpoint reader to construct arbitrary classes.
    request: dict
    session: dict
    run_id: str

