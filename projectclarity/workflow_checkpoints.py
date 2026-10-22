"""Keep LangGraph's place while a person reviews a research plan.

The CLI process can exit at review_plan. A later command opens this same SQLite
database and resumes the interrupted node, without repeating the planning call.
An opted-in extraction stage can make new bounded model calls after retrieval.
LangGraph's maintained SqliteSaver owns checkpoint storage; this file only
configures it. The database is local development infrastructure, not PostgreSQL
or a multi-host job service. Keep it with the run folders when moving the store.

Graph state uses plain JSON-shaped dictionaries. The restricted serializer does
not enable pickle fallback or arbitrary application-class reconstruction. Private
questions/plans and parsed extraction candidates live here. Credential settings,
model clients, callbacks and full SDK responses are not graph state. This is not
a general secret scanner for user-provided research text.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from projectclarity.research_planner import ResearchError


@contextmanager
def checkpoint_store(output_root: Path, *, create=False):
    """Open one local checkpoint store; never manufacture missing history on resume."""
    path = output_root / "workflow_checkpoints.sqlite"
    if not create and not path.is_file():
        raise ResearchError("No resumable checkpoint store exists. Older runs need a new research run.")
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        yield SqliteSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
    finally:
        connection.close()


def thread_config(run_id: str) -> dict:
    """Select this run's saved execution thread and bound graph steps to thirty.

    The recursion limit bounds LangGraph steps, not LLM calls or link depth.
    The one-hop loop uses its own depth/source/call limits. Thirty allows both
    rounds plus review; this framework guard is not permission for extra hops.
    """
    return {"configurable": {"thread_id": run_id}, "recursion_limit": 30}
