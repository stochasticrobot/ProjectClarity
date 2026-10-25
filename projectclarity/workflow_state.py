"""Checkpoint channel declarations shared by graph wiring and node implementations."""
from typing import TypedDict


class ResearchState(TypedDict, total=False):
    """Information carried between nodes: a request and session, never an API key."""

    # Durable channels contain only JSON-shaped data; node adapters validate
    # them into Python models. Checkpoints never reconstruct arbitrary classes.
    request: dict
    session: dict
    run_id: str

