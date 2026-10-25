"""Keep LangGraph's place while a person reviews a research plan.

The CLI process can exit at review_plan. A later command opens this same SQLite
database and resumes the interrupted node, without repeating the planning call.
Opted-in source selection, extraction, review and coverage stages can make new
bounded model calls after resumption; the original planning call is not repeated.
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

from projectclarity.nodes.plan_research.research_planner import ResearchError


@contextmanager
def checkpoint_store(output_root: Path, *, create=False):
    """Open one local checkpoint store; never manufacture missing history on resume.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        create: Whether to create the checkpoint directory/database; False requires an
            existing store for resume or inspection.

    Returns:
        A context manager yielding a SqliteSaver configured for JSON-compatible graph
        checkpoints; close its connection on exit.
    """
    path = output_root / "workflow_checkpoints.sqlite"
    if not create and not path.is_file():
        raise ResearchError("No resumable checkpoint store exists. Older runs need a new research run.")
    connection = sqlite3.connect(str(path), check_same_thread=False)
    try:
        yield SqliteSaver(connection, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
    finally:
        connection.close()


def thread_config(run_id: str) -> dict:
    """Select this run's saved execution thread and cap an invocation at 36030 graph steps so
    accidental graph loops cannot run indefinitely.

    The recursion limit counts LangGraph steps, not model calls or link depth. It permits
    up to twelve steps per model call (1000 maximum), six per acquisition
    attempt (three attempts for each of 1000 pages), plus retry-wait/setup headroom. Separate
    operator source, optional depth and request limits stop ordinary exploration earlier.

    Args:
        run_id: Saved run identifier naming one subdirectory under output_root.

    Returns:
        A LangGraph configuration dictionary selecting run_id as the checkpoint thread and
        limiting execution to 36030 graph steps as a final programming-error backstop.
    """
    return {"configurable": {"thread_id": run_id}, "recursion_limit": 36030}
