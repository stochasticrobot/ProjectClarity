"""Ask the model for a research plan and save the accepted plan or failure details.

The plan divides the user's question into goals, proposes investigation steps and
searches, and specifies evidence checks. If an answer specification was created
earlier, the plan must respect its report fields and requirements. This step runs
before normal source retrieval; it supplies source references, not document bodies.
It records request usage and failures, validates source references, and saves the
plan for human review or continued execution. It does not itself conduct the
planned research.
"""
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
from projectclarity.providers.model_retry import invoke_with_retries
from projectclarity.providers.model_failures import ModelCallFailure, describe_model_failure


def plan_research(state: ResearchState, output_root: Path, key_reader=read_api_key, monitor=None) -> dict:
    """Create the research plan after optional answer design and before reading sources so scope, goals, completion
    criteria and clarification needs are explicit.

    Enforce local request limits, invoke the planner with bounded transient recovery, validate its structured output
    and save the resulting plan or safe failure diagnosis. A proposed search is not
    permission to fetch its results. Convert initial estimates to the stored-plan
    format with application-owned evidence metadata before review or retrieval.
    Separate reserved provenance fields from item identities and journal the
    original shape; no extracted fact or evidence value is invented.
    A designed report binds goal questions, evidence requirements and required
    fields. New controller runs also require explicit auxiliary-goal mappings to
    accepted requirements. Missing goals, mappings or checks fail before research starts.

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
        if monitor:
            monitor.check_cancel()
        session.status = "planning"
        # The shared executor reserves every wire attempt before invocation.
        # Timeouts may incur usage; retries never replenish the request allowance.
        save_session(session, output_root)
        messages = planning_messages(session)
        if monitor:
            monitor.model_input("plan_research", "plan", messages, ResearchPlanOutput, api_key)
        response = invoke_with_retries(session, output_root, task='plan_research', call_id='plan',
            messages=messages, model_factory=lambda timeout: create_planner(
                api_key, session, timeout_seconds=timeout), monitor=monitor, secret=api_key)
        raw = response["raw"]
        if monitor:
            monitor.model_output("plan_research", "plan", raw, api_key)
        usage = raw.usage_metadata or {}
        for field in ('input_tokens', 'output_tokens', 'total_tokens'):
            value = usage.get(field)
            prior = 0 if session.usage.requests_attempted == 1 else getattr(session.usage, field)
            setattr(session.usage, field, prior + value if prior is not None and value is not None else None)
        session.usage.response_model = raw.response_metadata.get("model_name")
        session.usage.stop_reason = raw.response_metadata.get("stop_reason")
        if monitor:
            monitor.check_cancel()
        if session.usage.stop_reason != "end_turn":
            raise ResearchError("The planner did not finish its answer. No retry was made for this incomplete answer; consider a higher --max-output-tokens limit.")
        if response.get("parsing_error") is not None or response.get("parsed") is None:
            # LangChain's exception message can contain the entire rejected
            # answer. Persist only known field names and application-owned rules.
            planning_failure = describe_planning_failure(response.get("parsing_error"))
            write_text(output_root / session.session_id / "planning_failure.json",
                       planning_failure.model_dump_json(indent=2) + "\n")
            raise ResearchError("The planner returned an invalid plan. "
                + planning_failure.explanation()
                + " Nothing was accepted and no retry was made for this invalid answer.")
        plan = response["parsed"].to_plan()
        from projectclarity.answer_schema.binding import bind_report
        from projectclarity.nodes.schedule_work.answer_controller import enabled
        try:
            bind_report(plan, session.answer_schema,
                    require_mappings=enabled(session))
        except ValueError:
            raise ResearchError('The plan does not satisfy the answer contract: missing goal, requirement mapping or completion check. No plan was accepted.') from None
        from projectclarity.result_contract import separate_plan_provenance
        adjustments = separate_plan_provenance(plan)
        if adjustments:
            session.workflow['result_contract_adjustments'] = adjustments
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
    except ModelCallFailure as exc:
        session.status = 'failed'
        http = exc.diagnostics.get('http_status')
        detail = f' (HTTP {http})' if http is not None else ''
        session.error = f"Planning stopped: {exc.diagnostics.get('category', exc.code)}{detail} ({exc.code}). See model_attempts.json."
    except Exception as exc:
        session.status = "failed"
        # This session error is visible in normal displays, so include only
        # type/status here. The shared model executor separately retains private
        # provider failure details with credential redaction.
        diagnostics = describe_model_failure(exc)
        status = diagnostics['http_status']
        detail = f" (HTTP {status})" if isinstance(status, int) else ""
        session.error = f"Planning failed: {diagnostics['exception_type']}{detail}. No retry was made."
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
