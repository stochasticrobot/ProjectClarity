"""Invoke and save the initial research plan, including bounded failure handling."""
import time
from pathlib import Path
from projectclarity.workflow_state import ResearchState
from projectclarity.research_schemas import ResearchPlanOutput
from projectclarity.nodes.plan_research.research_planner import ResearchError, create_planner, planning_messages, read_api_key
from projectclarity.nodes.plan_research.planning_failure_diagnostics import describe_planning_failure
from projectclarity.plan_renderer import render_plan
from projectclarity.nodes.review_plan.plan_review_service import create_plan_review
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.nodes.assess_research_gaps.node import save_coverage
from projectclarity.session_storage import save_session, write_text


def plan_research(state: ResearchState, output_root: Path, key_reader=read_api_key, monitor=None) -> dict:
    """Create the research plan before reading source documents so scope, goals, completion
    criteria and clarification needs are explicit.

    Enforce local request limits, invoke the planner once, validate its structured output
    and save the resulting plan or safe failure diagnosis. A proposed search is not
    permission to fetch its results. Convert initial estimates to the stored-plan
    format with application-owned evidence metadata before review or retrieval.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        key_reader: Zero-argument callable returning the model API key when a request needs
            it; defaults to read_api_key.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    session.visited_nodes.append("plan_research")
    started = time.monotonic()
    planning_failure = None
    try:
        if monitor:
            monitor.observe("planning_started", stage="planning", run_status="planning")
            monitor.check_cancel()
        if session.usage.requests_attempted >= session.request.budget.max_requests:
            raise ResearchError("LLM request budget exhausted; no provider request was made.")
        api_key = key_reader()
        planner = create_planner(api_key, session)
        if monitor:
            monitor.check_cancel()
        session.status = "planning"
        # Reserve the attempt BEFORE invoking. A timeout may still incur usage;
        # it must not be mistaken for a free call or trigger an automatic retry.
        session.usage.requests_attempted += 1
        save_session(session, output_root)
        messages = planning_messages(session)
        if monitor:
            monitor.model_input("plan_research", "plan", messages, ResearchPlanOutput, api_key)
        response = planner.invoke(messages)
        raw = response["raw"]
        if monitor:
            monitor.model_output("plan_research", "plan", raw, api_key)
        usage = raw.usage_metadata or {}
        session.usage.input_tokens = usage.get("input_tokens")
        session.usage.output_tokens = usage.get("output_tokens")
        session.usage.total_tokens = usage.get("total_tokens")
        session.usage.response_model = raw.response_metadata.get("model_name")
        session.usage.stop_reason = raw.response_metadata.get("stop_reason")
        if monitor:
            monitor.check_cancel()
        if session.usage.stop_reason != "end_turn":
            raise ResearchError("The planner did not finish its answer. No retry was made; consider a higher --max-output-tokens limit.")
        if response.get("parsing_error") is not None or response.get("parsed") is None:
            # LangChain's exception message can contain the entire rejected
            # answer. Persist only known field names and application-owned rules.
            planning_failure = describe_planning_failure(response.get("parsing_error"))
            write_text(output_root / session.session_id / "planning_failure.json",
                       planning_failure.model_dump_json(indent=2) + "\n")
            raise ResearchError("The planner returned an invalid plan. "
                + planning_failure.explanation()
                + " Nothing was accepted and no retry was made.")
        plan = response["parsed"].to_plan()
        if not plan.completion_criteria:
            raise ResearchError("The planner omitted completion/coverage criteria. No plan was accepted.")
        known_sources = {source.source_id for source in session.sources}
        if any(source_id not in known_sources for step in plan.strategy for source_id in step.source_ids):
            raise ResearchError("The plan referenced a source ID that was not supplied. Nothing was accepted.")
        session.plan = plan
        session.status = "needs_clarification" if plan.clarifications else "plan_ready"
        if plan.clarifications and not session.request.plan_only:
            session.pending_review = create_plan_review(session)
    except (RunCancelled, KeyboardInterrupt, EOFError):
        session.status = "cancelled"
        session.error = "Cancelled. Token usage may be unknown if a request was already sent."
    except ResearchError as exc:
        session.status = "failed"
        session.error = str(exc)
    except Exception as exc:
        session.status = "failed"
        # Exception bodies can include prompts, headers or credentials. Store
        # only the type/status, never their arbitrary message or traceback.
        status = getattr(exc, "status_code", None)
        detail = f" (HTTP {status})" if isinstance(status, int) else ""
        session.error = f"Planning failed: {type(exc).__name__}{detail}. No retry was made."
    session.usage.elapsed_seconds = round(time.monotonic() - started, 3)
    # Human-readable snapshots are separate from LangGraph's durable checkpoint.
    save_session(session, output_root)
    if session.plan is not None:
        directory = output_root / session.session_id
        write_text(directory / "plan.json", session.plan.model_dump_json(indent=2) + "\n")
        write_text(directory / "plan.md", render_plan(session))
        save_coverage(session, output_root)
    if monitor:
        details = {"error_code": planning_failure.error_code} if planning_failure else {}
        monitor.observe("planning_finished", stage=session.status, run_status=session.status, **details)
    return {"session": session}


