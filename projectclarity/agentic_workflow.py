"""Define ProjectClarity's LangGraph: its named nodes and the routes between them.

START HERE to see how the application runs. This is the file where build_graph()
registers nodes with add_node() and connects them with edges. A node is one step
of work; an edge says which step comes next. LangGraph carries ResearchState
(the request and this run's session record) from one step to another.

The normal route is:
    START -> initialize_research -> plan_research -> review_plan -> retrieve_sources -> END
With --plan-only or a failed plan, the route ends after plan_research. Otherwise
review_plan either passes a clear plan onward or interrupts for a human decision.
It does not display a terminal prompt. The CLI (or a future web UI) handles that.

For example, a question plus source_urls.txt and report.pdf first becomes a plan.
The original plan ALWAYS comes before fetching a URL or reading a document's
contents. Initialization registers references only; unread sources are expected,
not a reason for the planner to demand source text or browsing permission.
Once the plan can proceed, retrieve_sources saves the supplied documents'
text and provenance. This is acquisition, not an answer to the research question.
With --extract, retrieval continues to extract_knowledge -> validate_evidence ->
persist_candidates. --review-claims adds review_claims -> adjudicate_claims.
--follow-links then adds select_followup_sources, which can return to retrieval
ONCE for registered, origin-restricted links. Review and selection use an LLM;
adjudication and evidence validation are ordinary Python. Extraction is defined in its own file under extraction/, not
inside this graph definition. Saved candidates are not approved facts.

File responsibilities:
    This file: define the graph, implement node-level decisions, update run status.
    source_reference_loader.py: check and label supplied URLs/file paths before planning.
    research_planner.py: prepare planning questions/source references and the output schema.
    planning_failure_diagnostics.py: explain rejected replies without exposing their contents.
    retrieval/: read/fetch sources, parse text, and produce per-source receipts.
    extraction/extract_knowledge_node.py: the extraction node and bounded call loop.
    extraction/validate_evidence_node.py: Python-only source/quote/reference checks.
    extraction/candidate_claim_storage.py: the persistence node and readable candidates.
    session_storage.py: save local snapshots; plan_renderer.py: make them readable.
    run_monitor.py: publish live events/status and observe operator stop requests.

Usage and limits:
    command_line.py calls run_research(); it compiles and invokes THIS graph. Source text
    goes into extraction/review prompts only with their explicit CLI permissions. workflow_checkpoints.py keeps
    LangGraph's place at review; session.json remains an inspectable projection.
    Resuming arbitrary failed retrieval tasks is not implemented.
    run_research() owns one local monitor/worker lock for the graph invocation;
    optional monitor arguments keep nodes callable independently in unit tests.
"""

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langsmith import tracing_context

from projectclarity.research_schemas import ResearchRequest, ResearchSession, ResearchPlan
from projectclarity.research_planner import ResearchError, create_planner, planning_messages, read_api_key
from projectclarity.planning_failure_diagnostics import describe_planning_failure
from projectclarity.run_graph_visualization import save_invoked_graph
from projectclarity.plan_renderer import render_plan
from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalLimits, RunCancelled
from projectclarity.retrieval.source_retrieval import retrieve_one, save_manifest
from projectclarity.retrieval.public_url_fetcher import PublicWebReader
from projectclarity.session_storage import save_session, write_text
from projectclarity.source_reference_loader import load_sources
from projectclarity.run_monitor import MonitoringError, RunMonitor
from projectclarity.plan_review_contracts import PlanReviewRequest, PlanReviewDecision
from projectclarity.plan_review_service import create_plan_review, validate_review_decision
from projectclarity.workflow_checkpoints import checkpoint_store, thread_config
from projectclarity.extraction.extract_knowledge_node import extract_knowledge
from projectclarity.extraction.validate_evidence_node import validate_evidence
from projectclarity.extraction.candidate_claim_storage import persist_candidates
from projectclarity.claim_review.review_claims_node import review_claims
from projectclarity.claim_review.adjudicate_claims_node import adjudicate_claims
from projectclarity.discovery.select_followup_sources_node import select_followup_sources
from projectclarity.discovery.observed_link_inventory import seed_origins


class ResearchState(TypedDict, total=False):
    """Information carried between nodes: a request and session, never an API key."""

    # Durable channels contain only JSON-shaped data; node adapters validate
    # them into Python models. Checkpoints never reconstruct arbitrary classes.
    request: dict
    session: dict
    run_id: str


def initialize_research(state: ResearchState, output_root: Path, monitor=None) -> dict:
    """Register source references, create a unique run folder and start monitoring.

    The node adapter supplies typed request data here. Only the URL-list file
    and local path metadata are read; actual document contents wait until after
    planning/review. The initial saved session makes early failures inspectable.
    """
    request = state["request"]
    sources = load_sources(request.urls_file, request.document_paths)
    session = ResearchSession(
        session_id=state.get("run_id") or uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        request=state["request"],
        mode="plan_only" if request.plan_only else "plan_retrieve_extract" if request.extract_knowledge else "plan_and_retrieve",
        sources=sources,
        visited_nodes=["initialize_research"],
    )
    # A unique directory isolates runs; no existing output is overwritten.
    (output_root / session.session_id).mkdir(parents=True, exist_ok=False)
    save_session(session, output_root)
    if monitor:
        monitor.start(session)
    return {"session": session}


def plan_research(state: ResearchState, output_root: Path, key_reader=read_api_key, monitor=None) -> dict:
    """Enforce local limits, call the planner once, validate, and save for review."""
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
            monitor.model_input("plan_research", "plan", messages, ResearchPlan, api_key)
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
        plan = response["parsed"]
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
    if monitor:
        details = {"error_code": planning_failure.error_code} if planning_failure else {}
        monitor.observe("planning_finished", stage=session.status, run_status=session.status, **details)
    return {"session": session}


def retrieve_sources(state: ResearchState, output_root: Path, monitor=None, runtime=None) -> dict:
    """Acquisition node: collect supplied documents, saving each outcome as we go.

    The model's search suggestions never become permission to fetch new URLs.
    We iterate only over registered source IDs: supplied seeds, then explicitly
    authorized observed links registered by the discovery node.
    Reading/parsing details belong to retrieval/, not to this graph definition.
    Each attempt reports progress and persists its outcome before the next starts.
    Source failures may continue; cancellation or a whole-run limit stops the loop.

    A graph invocation shares one reader across rounds, including counters,
    elapsed time, robots policies, pacing and HTTP-429 blocks. Only source IDs
    without receipts are attempted. Runtime clients never enter checkpoints.
    """
    session = state["session"]
    session.visited_nodes.append("retrieve_sources")
    session.status = "retrieving"
    directory = output_root / session.session_id
    runtime = runtime if runtime is not None else {}
    if "web" not in runtime:
        prior = session.retrieval_usage
        budget = RetrievalBudget(RetrievalLimits(**session.request.fetch_limits.model_dump()),
            bytes_read=prior.get("bytes_read", 0), http_requests=prior.get("http_requests", 0),
            started=time.monotonic() - prior.get("elapsed_seconds", 0),
            check_control=monitor.check_cancel if monitor else None,
            on_event=monitor.observe if monitor else None)
        runtime["web"] = PublicWebReader(budget)
    web = runtime["web"]
    budget = web.budget
    attempted_ids = {document.source_id for document in session.documents}
    save_session(session, output_root)
    try:
        if monitor:
            monitor.observe("retrieval_started", stage="retrieving", run_status="retrieving")
        for source in session.sources:
            if source.source_id in attempted_ids:
                continue
            # Discovered-page redirects must remain within the permission given
            # by --follow-links. Original supplied pages keep existing behavior.
            web.allowed_origins = seed_origins(session) if getattr(source, "depth", 0) == 1 else None
            if monitor:
                monitor.check_cancel()
                monitor.observe("source_started", source_id=source.source_id, stage="acquiring")
            record = retrieve_one(source, directory, web)
            session.documents.append(record)
            session.retrieval_usage = budget.summary()
            save_manifest(directory, session.documents)
            save_session(session, output_root)
            budget.emit("source_finished", source_id=source.source_id, source_status=record.status,
                        byte_count=record.byte_count, character_count=record.character_count,
                        text_path=record.text_path, error_code=record.error_code,
                        source_seconds=record.elapsed_seconds)
            if record.status == "cancelled":
                raise RunCancelled("Run cancellation requested.")
            if record.error_code in {"run_time_limit", "request_limit", "total_size_limit"}:
                # These apply to the whole run, not just a bad source. Do not
                # manufacture repeated failures for documents we cannot attempt.
                session.error = f"Retrieval stopped at {record.error_code}; remaining sources were not attempted."
                budget.emit("retrieval_limit_reached", error_code=record.error_code)
                break
        successes = sum(document.status == "retrieved" for document in session.documents)
        if not session.sources:
            session.status = "retrieval_failed"
            session.error = "No sources supplied. Add --urls or --document, or use --plan-only."
        elif successes == len(session.sources):
            session.status = "sources_retrieved"
        elif successes:
            session.status = "retrieval_partial"
        else:
            session.status = "retrieval_failed"
    except (RunCancelled, KeyboardInterrupt, EOFError):
        session.status = "cancelled"
        session.error = "Retrieval cancelled; already completed source receipts have been kept."
    except Exception as exc:
        session.status = "retrieval_failed"
        session.error = f"Retrieval could not finish ({type(exc).__name__}); inspect saved source receipts."
    session.retrieval_usage = budget.summary()
    save_manifest(directory, session.documents)
    save_session(session, output_root)
    write_text(directory / "plan.md", render_plan(session))
    return {"session": session}


def after_planning(state: ResearchState) -> str:
    """Send valid execution plans to review; failed/plan-only runs still end here."""
    session = state["session"]
    if session.request.plan_only or session.status not in {"plan_ready", "needs_clarification"}:
        return "stop"
    return "review"


def review_plan(state: ResearchState, output_root: Path, monitor=None) -> dict:
    """Pause for a UI decision when necessary, without embedding any UI code.

    LangGraph restarts this function when the decision arrives. Therefore all
    durable side effects occur AFTER interrupt(), which returns that decision
    on resume. The outer runner announces the pause after checkpointing succeeds.
    A plan without questions passes through; that is not recorded as human approval.
    """
    session = state["session"]
    if session.pending_review:
        review = PlanReviewRequest.model_validate(session.pending_review)
        decision = PlanReviewDecision.model_validate(interrupt(review.model_dump(mode="json")))
        validate_review_decision(review, decision, session)
        session.review_decision = decision.model_dump(mode="json")
        session.review_resolved_at = datetime.now(UTC).isoformat()
        session.pending_review = None
        if decision.revised_plan is not None:
            session.plan = decision.revised_plan
        session.status = "rejected" if decision.action == "reject" else "plan_ready"
        directory = output_root / session.session_id
        write_text(directory / "review_decision.json", json_text(session.review_decision))
        write_text(directory / "plan.json", session.plan.model_dump_json(indent=2) + "\n")
        if monitor:
            monitor.observe("review_resolved", review_id=decision.review_id, review_action=decision.action,
                            run_status=session.status, stage="review_plan")
    if "review_plan" not in session.visited_nodes:
        session.visited_nodes.append("review_plan")
    save_session(session, output_root)
    write_text(output_root / session.session_id / "plan.md", render_plan(session))
    return {"session": session}


def json_text(value: dict) -> str:
    """Serialize a private JSON artifact; telemetry receives only its identifiers."""
    import json
    return json.dumps(value, ensure_ascii=True, indent=2) + "\n"


def typed_state(state: ResearchState) -> dict:
    """Validate plain checkpoint data before a node uses any saved request/state."""
    result = dict(state)
    if "request" in state:
        result["request"] = ResearchRequest.model_validate(state["request"])
    if "session" in state:
        result["session"] = ResearchSession.model_validate(state["session"])
    return result


def build_graph(output_root: Path, key_reader=read_api_key, monitor=None, *, checkpointer=None):
    """THIS defines the LangGraph nodes and their order; compile makes it runnable.

    Each small lambda passes a node the output folder (and, for LLM steps, the
    credential reader). LangGraph supplies the state argument when that node runs.
    Conditional edges route to review, stop, retrieve, or opt-in extraction.
    SQLite persistence is supplied by the runner; the graph itself knows nothing
    about a terminal/web UI. Only explicit follow-links permission enables a one-hop loop.
    """
    builder = StateGraph(ResearchState)
    runtime = {}  # Ephemeral shared HTTP client/cache; never serialized as graph state.
    def node(name, function):
        """Announce the actual executing node while preserving plain checkpoint dictionaries."""
        def execute(state):
            """Report entry and normal return; an interrupted/raised node has no finish event."""
            if monitor:
                monitor.observe("node_started", node_name=name, stage=name)
            result = function(typed_state(state))["session"]
            if monitor:
                monitor.observe("node_finished", node_name=name)
            return {"session": result.model_dump(mode="json")}
        return execute
    builder.add_node("initialize_research", node("initialize_research", lambda state: initialize_research(state, output_root, monitor)))
    builder.add_node("plan_research", node("plan_research", lambda state: plan_research(state, output_root, key_reader, monitor)))
    builder.add_node("review_plan", node("review_plan", lambda state: review_plan(state, output_root, monitor)))
    builder.add_node("retrieve_sources", node("retrieve_sources", lambda state: retrieve_sources(state, output_root, monitor, runtime)))
    # The functions below live in dedicated files. This file only connects the
    # steps, making the graph readable without embedding extraction internals.
    builder.add_node("extract_knowledge", node("extract_knowledge", lambda state: extract_knowledge(state, output_root, key_reader, monitor)))
    builder.add_node("validate_evidence", node("validate_evidence", lambda state: validate_evidence(state, output_root, monitor)))
    builder.add_node("persist_candidates", node("persist_candidates", lambda state: persist_candidates(state, output_root, monitor)))
    builder.add_node("review_claims", node("review_claims", lambda state: review_claims(state, output_root, key_reader, monitor)))
    builder.add_node("adjudicate_claims", node("adjudicate_claims", lambda state: adjudicate_claims(state, output_root, monitor)))
    builder.add_node("select_followup_sources", node("select_followup_sources", lambda state: select_followup_sources(
        state, output_root, key_reader, monitor, runtime.get("web"))))
    builder.add_edge(START, "initialize_research")
    builder.add_edge("initialize_research", "plan_research")
    builder.add_conditional_edges("plan_research", lambda state: after_planning(typed_state(state)), {"stop": END, "review": "review_plan"})
    builder.add_conditional_edges("review_plan", lambda state: "retrieve" if state["session"]["status"] == "plan_ready" else "stop",
                                  {"retrieve": "retrieve_sources", "stop": END})
    builder.add_conditional_edges("retrieve_sources",
        lambda state: "extract" if state["session"]["request"].get("extract_knowledge", False)
            and state["session"]["status"] in {"sources_retrieved", "retrieval_partial"} else "stop",
        {"extract": "extract_knowledge", "stop": END})
    builder.add_edge("extract_knowledge", "validate_evidence")
    builder.add_edge("validate_evidence", "persist_candidates")
    builder.add_conditional_edges("persist_candidates",
        lambda state: "review" if state["session"]["request"].get("review_claims", False)
            and state["session"]["status"] != "cancelled" else "stop",
        {"review": "review_claims", "stop": END})
    builder.add_edge("review_claims", "adjudicate_claims")
    builder.add_conditional_edges("adjudicate_claims",
        lambda state: "discover" if state["session"]["request"].get("follow_links", False)
            and state["session"]["status"] != "cancelled" else "stop",
        {"discover": "select_followup_sources", "stop": END})
    builder.add_conditional_edges("select_followup_sources",
        lambda state: "retrieve" if state["session"]["discovery"].get("stop_reason") == "followup_registered"
            and state["session"]["status"] != "cancelled" else "stop",
        {"retrieve": "retrieve_sources", "stop": END})
    return builder.compile(checkpointer=checkpointer)


def finish_graph_invocation(result: dict, output_root: Path, monitor: RunMonitor, graph) -> ResearchSession:
    """Save the returned outcome and actual graph, or announce a durably saved pause.

    A pause is not completion. Waiting for a person leaves the checkpoint ready
    for review continuation; a normal return closes monitoring, even on a
    controlled failed/partial result. The text graph records visits, not success.
    """
    session = ResearchSession.model_validate(result["session"])
    if result.get("__interrupt__"):
        session.status = "awaiting_review"
        if "review_plan" not in session.visited_nodes:
            session.visited_nodes.append("review_plan")
        save_session(session, output_root)
        directory = output_root / session.session_id
        write_text(directory / "review_request.json", json_text(session.pending_review))
        write_text(directory / "plan.md", render_plan(session))
        monitor.pause_for_review(session.pending_review)
    else:
        monitor.finish(session.status)
    # Export this exact compiled graph, not a separate diagram of what we think
    # is connected. Paused review is visited but not a completed invocation.
    save_invoked_graph(graph, session, output_root, finished=not bool(result.get("__interrupt__")))
    return session


def run_research(request: ResearchRequest, output_root: Path, key_reader=read_api_key, *, on_event=None) -> ResearchSession:
    """Start a new run and return its completed, failed or review-paused session.

    invoke() is the call that actually runs the graph; build_graph() only builds
    it. Remote tracing stays disabled; local events and private extraction traces
    remain available. This runner holds the worker lock only during execution,
    not while a person is deciding whether to resume a paused plan.
    """
    monitor = RunMonitor(output_root, sink=on_event)
    try:
        # Preflight input references before creating a checkpoint database. No
        # document bodies are read. Initialization rechecks them in the graph.
        load_sources(request.urls_file, request.document_paths)
        output_root.mkdir(parents=True, exist_ok=True)
        run_id = uuid4().hex
        with monitor.termination_handler(), tracing_context(enabled=False), checkpoint_store(output_root, create=True) as store:
            graph = build_graph(output_root, key_reader, monitor, checkpointer=store)
            result = graph.invoke(
                {"request": request.model_dump(mode="json"), "run_id": run_id}, thread_config(run_id))
            return finish_graph_invocation(result, output_root, monitor, graph)
    except BaseException as exc:
        try:
            monitor.finish("cancelled" if isinstance(exc, (RunCancelled, KeyboardInterrupt, EOFError)) else "failed",
                           error_type=type(exc).__name__)
        except MonitoringError:
            pass  # Preserve the original failure; lock release still exposes a dead worker.
        raise
    finally:
        monitor.close()
