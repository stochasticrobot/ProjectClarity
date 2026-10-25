"""Print the connected steps recorded for a research run, using LangGraph's topology.

For example, a completed download run shows START -> initialize -> plan -> review
-> retrieve -> END, while a paused review stops before retrieval. Nodes and edges
come from get_graph(), not a second hand-maintained workflow definition.

The workflow saves workflow_graph.json and workflow_graph.txt when an invocation
finishes or pauses. `projectclarity graph RUN_ID` prints a view using that captured
definition and the saved session. Older runs without a captured graph explicitly
say they use a reconstruction from current code. JSON keeps the data separate
from presentation, so a future HTML UI need not redefine the workflow.

No browser, network, model call, or extra diagram library is required. This is a
static view, not live tracing. A visited node need not have succeeded. Edges in
the recorded route are inferred from the current sequential workflow's ordered
visited_nodes; repeated visits preserve search and recursive-link loop order, but parallel timing and
individual calls within a node are not represented by this record.
"""

import json
from pathlib import Path

from projectclarity.research_schemas import ResearchSession
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.run_status_commands import run_directory
from projectclarity.session_storage import write_text


def graph_definition(graph) -> dict:
    """Extract actual node IDs and edges from the compiled LangGraph, without running it.

    Args:
        graph: Compiled LangGraph whose topology is inspected without invocation.

    Returns:
        A JSON-compatible dictionary of registered nodes and edges from the compiled
        workflow.
    """
    native = graph.get_graph().to_json()
    return {"nodes": [node["id"] for node in native["nodes"]],
            "edges": [{"source": edge["source"], "target": edge["target"],
                       "label": edge.get("data", ""), "conditional": edge.get("conditional", False)}
                      for edge in native["edges"]]}


def run_snapshot(session: ResearchSession, definition: dict, origin: str, *, finished: bool) -> dict:
    """Combine topology with recorded progress; exclude prompts, URLs and source text.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        definition: Node/edge dictionary extracted from the compiled workflow.
        origin: Provenance label for the topology, such as captured or reconstructed; not a
            web origin.
        finished: Whether this invocation has ended, as distinct from a snapshot taken
            during execution or a review pause.

    Returns:
        A JSON-compatible topology/progress snapshot with actual visits, counters and
        execution status.
    """
    return {"schema_version": "1.0", "run_id": session.session_id, "status": session.status,
            "definition_origin": origin, "definition": definition,
            "visited_nodes": session.visited_nodes, "invocation_finished": finished,
            "human_decision": (session.review_decision or {}).get("action"),
            "sources_total": len(session.sources),
            "sources_retrieved": sum(doc.status == "retrieved" for doc in session.documents)}


def label(value) -> str:
    """Keep a graph label on one terminal line, even if saved data was edited.

    Args:
        value: Saved label value to render safely in the text graph.

    Returns:
        One printable graph-label line capped to 160 characters.
    """
    return "".join(char for char in str(value) if char.isprintable())[:160]


def render_graph_text(snapshot: dict) -> str:
    """Draw the recorded route vertically; list other edges separately, not as executed.

    Args:
        snapshot: Saved topology/progress dictionary produced by run_snapshot.

    Returns:
        Plain-text workflow diagram distinguishing recorded visits from unexecuted edges.
    """
    definition = snapshot["definition"]
    visits = snapshot["visited_nodes"]
    route = (["__start__"] if visits else []) + visits
    if snapshot["invocation_finished"] and visits:
        route.append("__end__")
    edges = {(edge["source"], edge["target"]): edge for edge in definition["edges"]}
    taken = set(zip(route, route[1:]))
    lines = ["Workflow graph", f"Run: {label(snapshot['run_id'])}",
             f"Status: {label(snapshot['status'])} | Sources retrieved: {snapshot['sources_retrieved']}/{snapshot['sources_total']}"]
    if snapshot["definition_origin"] == "captured":
        lines.append("Definition: captured from the compiled graph used for the latest invocation.")
    else:
        lines.append("Definition: reconstructed from CURRENT code; original topology was not saved.")
    lines.extend(["", "Recorded route (visited does not mean successful):"])
    last = visits[-1] if visits else None
    for index, node in enumerate(route):
        if index:
            edge = edges.get((route[index - 1], node))
            connector = label(edge["label"]) if edge else "edge absent from this definition"
            lines.extend(["    |" + (f" {connector}" if connector else ""), "    v"])
        name = {"__start__": "START", "__end__": "END"}.get(node, node)
        note = ""
        if node == last and snapshot["status"] in {"failed", "retrieval_failed", "retrieval_partial", "extraction_failed", "extraction_partial", "cancelled", "rejected", "awaiting_review"}:
            note = " -- " + ("PAUSED for human input" if snapshot["status"] == "awaiting_review" else label(snapshot["status"]))
        elif node == "review_plan":
            action = snapshot["human_decision"]
            note = f" -- human decision: {label(action)}" if action else " -- passed through; no human approval"
        lines.append(f"[{label(name)}]{note}")
    if not route:
        lines.append("No recorded node visits.")
    other_edges = [edge for edge in definition["edges"] if (edge["source"], edge["target"]) not in taken]
    if other_edges:
        lines.extend(["", "Other available edges (not recorded as taken):"])
        for edge in other_edges:
            source = {"__start__": "START", "__end__": "END"}.get(edge["source"], edge["source"])
            target = {"__start__": "START", "__end__": "END"}.get(edge["target"], edge["target"])
            condition = f" --{label(edge['label'])}--> " if edge["label"] else " --> "
            lines.append(f"  {label(source)}{condition}{label(target)}")
    lines.extend(["", "Snapshot only; route inferred from recorded node order, not a timed execution trace."])
    return "\n".join(lines) + "\n"


def save_graph_snapshot(directory: Path, snapshot: dict) -> Path:
    """Save reusable topology/progress data and its plain-text presentation.

    Args:
        directory: Local filesystem directory for this one saved research run.
        snapshot: Topology/progress dictionary to persist and render.

    Returns:
        The Path of workflow_graph.txt; also save the corresponding reusable JSON snapshot.
    """
    write_text(directory / "workflow_graph.json", json.dumps(snapshot, indent=2) + "\n")
    destination = directory / "workflow_graph.txt"
    write_text(destination, render_graph_text(snapshot))
    return destination


def save_invoked_graph(graph, session: ResearchSession, output_root: Path, *, finished: bool) -> Path:
    """Use the graph actually invoked, both for first execution and review resumption.

    Args:
        graph: Compiled graph actually used for the run or review continuation.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        finished: Whether this invocation has ended, as distinct from a snapshot taken
            during execution or a review pause.

    Returns:
        The Path of the saved workflow_graph.txt for this invocation.
    """
    return save_graph_snapshot(output_root / session.session_id,
        run_snapshot(session, graph_definition(graph), "captured", finished=finished))


def export_saved_run_graph(output_root: Path, run_id: str) -> Path:
    """Read a saved run without invoking nodes or reading credentials/documents.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        run_id: Saved run identifier naming one subdirectory under output_root.

    Returns:
        The Path of the regenerated workflow_graph.txt, labeled reconstructed when no
        captured topology exists.
    """
    directory = run_directory(output_root, run_id)
    try:
        session = ResearchSession.model_validate_json((directory / "session.json").read_text(encoding="utf-8"))
        if session.session_id != run_id:
            raise ValueError("Mismatched run")
        saved_path = directory / "workflow_graph.json"
        if saved_path.exists():
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            if saved["schema_version"] != "1.0" or saved["run_id"] != run_id:
                raise ValueError("Mismatched graph snapshot")
            definition, origin = saved["definition"], saved["definition_origin"]
            # If a worker has advanced since capture, do not reuse an old END
            # marker for its newer partial state. The next completed invocation
            # will replace the capture with its own definitive outcome.
            finished = (saved["invocation_finished"] and saved["status"] == session.status
                        and saved["visited_nodes"] == session.visited_nodes)
        else:
            # Compilation exposes topology; it never invokes nodes or reads a key.
            from projectclarity.agentic_workflow import build_graph
            definition, origin = graph_definition(build_graph(output_root)), "reconstructed_current_code"
            finished = session.status in {"sources_retrieved", "retrieval_partial", "retrieval_failed", "candidates_saved", "extraction_partial", "extraction_failed", "failed", "cancelled", "rejected"}
            finished = finished or (session.request.plan_only and session.status in {"plan_ready", "needs_clarification"})
        return save_graph_snapshot(directory, run_snapshot(session, definition, origin, finished=finished))
    except (OSError, ValueError, KeyError, TypeError):
        raise ResearchError("Cannot export this run's graph. Check its saved session, graph snapshot and file permissions.") from None
