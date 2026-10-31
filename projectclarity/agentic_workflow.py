"""Define and run ProjectClarity's research workflow using LangGraph.

LangGraph is the library that connects processing steps and follows rules for
choosing the next step. A step is called a node; a connection is called an edge.
This file registers the nodes and edges in build_graph(), then starts the compiled
workflow in run_research() or continues saved work in resume_research(). The
command-line interface uses these entry points, including dashboard-launched runs.

For example, a new run registers the supplied documents, optionally asks a model
what the answer should contain, creates a research plan, and handles any human
clarifications. It then repeatedly chooses work: download a source, read a passage
with a model, check quotations, review claims, find more sources, or assess whether
the collected evidence answers the question. The optional early coverage study
samples sources before that repeated work begins.

This file connects those operations; their implementations live in nodes/. The
scheduler chooses eligible work and enforces the run's remaining allowances.
Extraction can read several passages concurrently, but validation and review still
follow extraction. A finished task is not proof of a complete or correct answer.

The runners also manage worker locks, progress reporting and saved results.
SQLite checkpoints are saved when an invocation ends or pauses for human review,
not after every step. session.json records
scheduled work and spent allowances for other restarts. A model request whose
outcome is unknown after interruption is not automatically sent again. Live
clients and credentials stay outside the saved workflow state.
"""

import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from projectclarity.research_schemas import ResearchRequest, ResearchSession
from projectclarity.nodes.plan_research.research_planner import ResearchError, read_api_key
from projectclarity.run_graph_visualization import save_invoked_graph
from projectclarity.plan_renderer import render_plan
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import save_session, write_text
from projectclarity.nodes.accumulate_knowledge.node import accumulate_knowledge
from projectclarity.run_monitor import MonitoringError, RunMonitor
from projectclarity.workflow_checkpoints import checkpoint_store, thread_config
from projectclarity.nodes.extract_knowledge.node import extract_knowledge
from projectclarity.nodes.validate_evidence.node import validate_evidence
from projectclarity.nodes.persist_candidates.node import persist_candidates
from projectclarity.nodes.review_claims.node import review_claims
from projectclarity.nodes.adjudicate_claims.node import adjudicate_claims
from projectclarity.nodes.select_followup_sources.node import select_followup_sources
from projectclarity.nodes.web_search.node import web_search
from projectclarity.nodes.select_sources.node import select_sources
from projectclarity.nodes.assess_research_gaps.node import assess_research_gaps, save_coverage
from projectclarity.nodes.initialize_research.source_reference_loader import load_sources
from projectclarity.workflow_state import ResearchState
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.nodes.initialize_research.node import initialize_research
from projectclarity.nodes.plan_research.node import plan_research
from projectclarity.nodes.review_plan.node import review_plan, json_text
from projectclarity.nodes.retrieve_sources.node import retrieve_sources
from projectclarity.nodes.schedule_work.node import schedule
from projectclarity.nodes.wait_for_retry.node import wait_for_retry
from projectclarity.nodes.study_coverage.node import study_coverage, save_study
from projectclarity.nodes.design_answer.node import design_answer


def entry_route(state):
    """Choose where the workflow starts from the supplied run state.

    A missing session starts initialization. An existing session normally returns to
    task scheduling, except when an enabled preliminary coverage study has not started.

    Args:
        state: Graph input with an optional serialized or typed saved session.
    Returns:
        New, resume, or study routing label. Only a study never started may run
        on resume; recorded in-progress studies are recovered without replay.
    """
    if not state.get('session'):
        return 'new'
    session = ResearchSession.model_validate(state['session'])
    return 'study' if session.request.coverage_study and not session.coverage_study else 'resume'


def after_planning(state: ResearchState) -> str:
    """Choose whether the newly generated research plan proceeds to human review.

    Plan-only requests and failed plans stop here. An executable plan that is ready
    or needs clarification is sent to the review step, which decides whether to pause.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.

    Returns:
        The routing label review for a valid execution plan, or stop for failed or plan-only
        work.
    """
    session = state["session"]
    if session.request.plan_only or session.status not in {"plan_ready", "needs_clarification"}:
        return "stop"
    return "review"


def typed_state(state: ResearchState) -> dict:
    """Validate plain checkpoint data before a node uses any saved request/state.

    Args:
        state: Checkpoint-compatible dictionary whose request and session entries may be
            plain dictionaries or already typed models.

    Returns:
        A shallow copy of the state with request/session values validated into their
        application models when present.
    """
    result = dict(state)
    if "request" in state:
        result["request"] = ResearchRequest.model_validate(state["request"])
    if "session" in state:
        result["session"] = ResearchSession.model_validate(state["session"])
    return result


def build_graph(output_root: Path, key_reader=read_api_key, monitor=None, *, checkpointer=None):
    """Define the research steps and their connections, then compile a LangGraph workflow.

    LangGraph calls a registered function for each step and follows fixed or conditional
    connections to select the next one. This function registers initialization, optional
    answer design, planning, human review and the repeated research operations. A central
    scheduling step chooses eligible work within the run limits. Wrappers provide the
    output directory, credential callback and monitor, and convert session objects to
    plain dictionaries for checkpoints. Building the graph does not execute any steps.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        key_reader: Zero-argument callable returning the model API key when a request needs
            it; defaults to read_api_key.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.
        checkpointer: Optional LangGraph checkpoint saver supplied by the runner for durable
            execution and review resumes.

    Returns:
        A compiled LangGraph workflow ready to invoke; constructing it does not execute
        research.
    """
    builder = StateGraph(ResearchState)
    runtime = {'output_root': output_root}  # Keep live readers outside saved graph state.
    def node(name, function):
        """Announce the actual executing node while preserving plain checkpoint dictionaries.

        Args:
            name: Registered graph-node name used for progress events.
            function: Node callable accepting typed state and returning an updated
                ResearchSession under session.

        Returns:
            A callable that announces execution and adapts typed node results to serializable
            graph state.
        """
        def execute(state):
            """Report entry and normal return; an interrupted/raised node has no finish event.

            Args:
                state: Graph state supplied by LangGraph, converted to typed request/session models
                    before the node runs.

            Returns:
                A state-update dictionary whose session entry is JSON-compatible data for LangGraph
                checkpoints.
            """
            if monitor:
                monitor.observe("node_started", node_name=name, stage=name)
            result = function(typed_state(state))["session"]
            if monitor:
                monitor.observe("node_finished", node_name=name)
            return {"session": result.model_dump(mode="json")}
        return execute
    builder.add_node("initialize_research", node("initialize_research", lambda state: initialize_research(state, output_root, monitor)))
    builder.add_node("plan_research", node("plan_research", lambda state: plan_research(state, output_root, key_reader, monitor)))
    builder.add_node('design_answer', node('design_answer', lambda state: design_answer(state, output_root, key_reader, monitor)))
    builder.add_node("review_plan", node("review_plan", lambda state: review_plan(state, output_root, monitor)))
    builder.add_node('study_coverage', node('study_coverage', lambda state: study_coverage(
        state, output_root, key_reader, monitor, runtime)))
    builder.add_node("web_search", node("web_search", lambda state: web_search(state, output_root, monitor,
        retrieval_runtime(state['session'], runtime, monitor))))
    builder.add_node("select_sources", node("select_sources", lambda state: select_sources(state, output_root, key_reader, monitor)))
    builder.add_node("assess_research_gaps", node("assess_research_gaps", lambda state: assess_research_gaps(state, output_root, key_reader, monitor)))
    builder.add_node("retrieve_sources", node("retrieve_sources", lambda state: retrieve_sources(state, output_root, monitor, runtime)))
    builder.add_node("accumulate_knowledge", node("accumulate_knowledge", lambda state: accumulate_knowledge(state, output_root, key_reader, monitor, final_pass=True, max_calls=state['session'].workflow['tasks'][state['session'].workflow['active']].get('max_calls', 1))))
    # The functions below live in dedicated files. This file only connects the
    # steps, making the graph readable without embedding extraction internals.
    builder.add_node("extract_knowledge", node("extract_knowledge", lambda state: extract_knowledge(state, output_root, key_reader, monitor, max_batches=state['session'].request.model_concurrency)))
    builder.add_node("validate_evidence", node("validate_evidence", lambda state: validate_evidence(state, output_root, monitor)))
    builder.add_node("persist_candidates", node("persist_candidates", lambda state: persist_candidates(state, output_root, monitor)))
    builder.add_node("review_claims", node("review_claims", lambda state: review_claims(state, output_root, key_reader, monitor)))
    builder.add_node("adjudicate_claims", node("adjudicate_claims", lambda state: adjudicate_claims(state, output_root, monitor)))
    builder.add_node("select_followup_sources", node("select_followup_sources", lambda state: select_followup_sources(
        state, output_root, key_reader, monitor, runtime.get("web"))))
    builder.add_conditional_edges(START, entry_route,
        {'resume': 'schedule_work', 'new': 'initialize_research', 'study': 'study_coverage'})
    builder.add_conditional_edges('initialize_research',
        lambda state: 'design' if state['session']['request'].get('design_answer') else 'plan',
        {'design': 'design_answer', 'plan': 'plan_research'})
    builder.add_conditional_edges('design_answer',
        lambda state: 'plan' if state['session'].get('answer_schema') else 'stop',
        {'plan': 'plan_research', 'stop': END})
    builder.add_conditional_edges("plan_research", lambda state: after_planning(typed_state(state)), {"stop": END, "review": "review_plan"})


    builder.add_node('schedule_work', node('schedule_work', lambda state: schedule(state, output_root, monitor, runtime)))
    builder.add_node('wait_for_retry', node('wait_for_retry', lambda state: wait_for_retry(state, output_root, monitor, runtime)))
    builder.add_conditional_edges('review_plan',
        lambda state: ('study' if state['session']['request'].get('coverage_study') else 'schedule')
            if state['session']['status'] == 'plan_ready' else 'stop',
        {'schedule': 'schedule_work', 'study': 'study_coverage', 'stop': END})
    builder.add_conditional_edges('study_coverage',
        lambda state: 'stop' if state['session']['status'] == 'cancelled' else 'schedule',
        {'schedule': 'schedule_work', 'stop': END})
    actions = {name: name for name in ('web_search', 'select_sources', 'retrieve_sources',
        'extract_knowledge', 'validate_evidence', 'review_claims', 'assess_research_gaps',
        'select_followup_sources', 'accumulate_knowledge')}
    builder.add_conditional_edges('schedule_work', lambda state: state['session']['workflow']['next'],
        {**actions, 'wait': 'wait_for_retry', 'stop': END})
    for name in ('web_search', 'select_sources', 'retrieve_sources', 'assess_research_gaps',
                 'select_followup_sources', 'accumulate_knowledge', 'wait_for_retry'):
        builder.add_edge(name, 'schedule_work')
    builder.add_edge('extract_knowledge', 'validate_evidence')
    builder.add_edge('validate_evidence', 'persist_candidates')
    builder.add_conditional_edges('persist_candidates',
        lambda state: 'adjudicate' if state['session']['request'].get('review_claims')
            else 'schedule',
        {'adjudicate': 'adjudicate_claims', 'schedule': 'schedule_work'})
    builder.add_edge('review_claims', 'adjudicate_claims')
    builder.add_edge('adjudicate_claims', 'schedule_work')
    return builder.compile(checkpointer=checkpointer)


def finish_graph_invocation(result: dict, output_root: Path, monitor: RunMonitor, graph) -> ResearchSession:
    """Save the state returned by a workflow invocation and announce its outcome.

    An invocation can finish, fail, or pause for human clarification. This helper saves
    the relevant plan, graph view and answer exports before updating monitoring, so
    readers do not receive a finished event ahead of the corresponding saved results.

    Args:
        result: Dictionary returned by graph invocation, including session data and any
            native interrupt.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        monitor: Active RunMonitor that records the final or paused status after exports
            are saved. This function requires a monitor; None is not supported.
        graph: Compiled graph actually invoked, used to record its topology and inspect a
            review pause.

    Returns:
        The ResearchSession describing the finished, failed, partial or durably paused run.
    """
    session = ResearchSession.model_validate(result["session"])
    if session.extraction and session.status not in {'cancelled', 'awaiting_review', 'failed'}:
        session.extraction['retrieval_incomplete'] = (len(session.documents) != len(session.sources)
            or any(d.status != 'retrieved' for d in session.documents))
        persist_candidates({'session': session}, output_root, record_visit=False)
        if session.status == 'candidates_saved':
            session.error = None
    if session.workflow.get('stop_reason') in {'blocked', 'budget_exhausted'} and session.status not in {'cancelled', 'failed'}:
        session.status = 'extraction_partial' if session.extraction else 'retrieval_partial' if any(d.status == 'retrieved' for d in session.documents) else 'retrieval_failed'
        session.error = 'Workflow stopped with unfinished work: ' + ', '.join(session.workflow.get('stop_details', [])) + '.'
    if session.status not in {'cancelled', 'awaiting_review', 'failed'}:
        from projectclarity.providers.validation_diagnostics import unresolved_failures
        failed_calls = sorted({(c['task'], c['error_code']) for c in unresolved_failures(session)
            if not (c['task'] == 'accumulate_knowledge' and session.request.knowledge_max_requests is not None)})
        if failed_calls:
            session.status = 'extraction_partial' if session.extraction else 'retrieval_partial'
            details = '; '.join(f'{task}: {code}' for task, code in failed_calls)
            session.error = 'Incomplete model tasks (' + details + '). Saved evidence and failure receipts are retained.'
        discovery_errors = {'invalid_selection_references', 'invalid_output_schema', 'incomplete_model_output',
            'selection_failed', 'input_too_large', 'frontier_context_blocked'}
        failed_selections = sorted({d.get('stop_reason') for d in
            [session.discovery, *session.discovery.get('history', [])] if d.get('stop_reason') in discovery_errors})
        if failed_selections:
            session.status = 'extraction_partial' if session.extraction else 'retrieval_partial'
            session.error = (session.error + ' ' if session.error else '') + 'Follow-up selection incomplete: ' + ', '.join(failed_selections) + '.'
        failed_coverage = sorted({entry['error_code'] for entry in session.coverage.get('history', [])
            if entry.get('error_code')})
        if failed_coverage:
            session.status = 'extraction_partial' if session.extraction else 'retrieval_partial'
            session.error = (session.error + ' ' if session.error else '') + 'Coverage assessments incomplete: ' + ', '.join(failed_coverage) + '.'
    if session.request.web_search and session.status not in {"cancelled", "awaiting_review", "failed"}:
        failures = any(q.get("status") == "failed" for q in session.search.get("queries", [])) or any(
            s.get("error_code") for s in session.search.get("selections", []))
        if failures:
            session.status = "extraction_partial" if session.extraction else "retrieval_partial" if session.documents else "retrieval_failed"
            warning = "Search or source selection was incomplete; retained work is available in search.json."
            session.error = (session.error + " " if session.error else "") + warning
    if session.workflow.get('stop_reason') == 'criteria_satisfied' and session.status not in {
            'cancelled', 'awaiting_review', 'failed'}:
        from projectclarity.answer_schema.completion import contract_checks
        from projectclarity.coverage.stopping import completion_ready
        if contract_checks(session) and completion_ready(session, output_root):
            session.status = 'candidates_saved'
            session.error = None
    if result.get("__interrupt__"):
        session.status = "awaiting_review"
        if "review_plan" not in session.visited_nodes:
            session.visited_nodes.append("review_plan")
        save_session(session, output_root)
        directory = output_root / session.session_id
        write_text(directory / "review_request.json", json_text(session.pending_review))
        write_text(directory / "plan.md", render_plan(session))
    # Export this exact compiled graph, not a separate diagram of what we think
    # is connected. Paused review is visited but not a completed invocation.
    save_coverage(session, output_root)
    save_invoked_graph(graph, session, output_root, finished=not bool(result.get("__interrupt__")))
    from projectclarity.provisional_answer import save_provisional_answer
    save_provisional_answer(session, output_root)
    # Save answer and graph files before emitting the finished/paused event,
    # so a dashboard reading that event can immediately open the corresponding files.
    if result.get('__interrupt__'):
        monitor.pause_for_review(session.pending_review)
    else:
        monitor.finish(session.status)
    return session


def run_research(request: ResearchRequest, output_root: Path, key_reader=read_api_key, *, on_event=None) -> ResearchSession:
    """Start a new run and return its completed, failed or review-paused session.

    invoke() is the call that actually runs the graph; build_graph() only builds it. Remote
    tracing stays disabled; local events and private extraction traces remain available.
    This runner holds the worker lock only during execution, not while a person is deciding
    whether to resume a paused plan. LangGraph saves one checkpoint on exit, including a
    review pause or handled exception, instead of retaining a growing session copy after
    every step. An abrupt process death relies on the separately saved session journal;
    it does not have a new per-step graph checkpoint.

    Args:
        request: Validated ResearchRequest specifying the question, inputs, opt-in stages
            and resource limits.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        key_reader: Zero-argument callable returning the model API key when a request needs
            it; defaults to read_api_key.
        on_event: Optional callback receiving operational progress-event dictionaries, not
            source text or private prompts.

    Returns:
        The ResearchSession holding results and status for this new run, including failure
        or a pending review pause.
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
                {"request": request.model_dump(mode="json"), "run_id": run_id},
                thread_config(run_id), durability="exit")
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


def resume_research(run_id: str, output_root: Path, key_reader=read_api_key, *, on_event=None) -> ResearchSession:
    """Continue a scheduler journal without replanning, resetting limits or replaying calls.

    Args:
        run_id: Existing run directory identifier, validated against traversal.
        output_root: Local directory containing that run and its checkpoint store.
        key_reader: Credential reader used only if remaining work needs a model.
        on_event: Optional operational progress sink, never source/prompt content.

    Returns:
        Updated session after remaining work. Refuse legacy journals, unresolved
        plan reviews, and explicitly cancelled runs. An interrupted task is marked
        uncertain; completed saved evidence can still be finalized. A worker lock
        prevents concurrent resumptions. Interrupted early studies are not replayed;
        started wire attempts keep their charge and have unknown token usage.
        Reserved bounded summaries, including corrections, become uncertain before
        scheduling so neither unchanged nor repartitioned work can replay them.
        Existing budgets and breaker states stay. Recovery reads session.json, not
        intermediate graph checkpoints; the resumed invocation saves its graph
        checkpoint only when it exits or pauses.
    """
    from projectclarity.run_status_commands import run_directory
    directory = run_directory(output_root, run_id)
    monitor = RunMonitor(output_root, sink=on_event)
    try:
        monitor.attach(directory)
        session = ResearchSession.model_validate_json((directory / 'session.json').read_text())
        if session.workflow.get('version') != 1 or not session.plan:
            raise ResearchError('This run has no compatible workflow journal; start a new run.')
        if session.pending_review or session.status in {'cancelled', 'rejected'}:
            raise ResearchError('Plan review or cancellation prevents automatic workflow resume.')
        active = session.workflow.get('active')
        for batch_id, receipt in session.coverage.get('evidence_batches', {}).items():
            if batch_id.startswith('BT_') and receipt.get('status') == 'reserved':
                receipt.update(status='uncertain', error_code='interrupted_outcome_unknown')
                if receipt.get('attempt_history'):
                    receipt['attempt_history'][-1].update(status='uncertain',
                        error_code='interrupted_outcome_unknown')
        if session.coverage_study.get('status') == 'running':
            if not active and session.retrieval_usage:
                saved_at = session.retrieval_usage.get('checkpoint_wall_time', time.time())
                session.retrieval_usage['elapsed_seconds'] += max(0, time.time() - saved_at)
            session.coverage_study.update(status='interrupted', phase='interrupted', error_code='interrupted_outcome_unknown')
            for call in session.processing_calls:
                if call['task'] == 'study_coverage' and call['status'] in {'started', 'failed'} and not call.get('error_code'):
                    call.update(status='failed', error_code='interrupted_outcome_unknown')
            for attempt in session.model_attempts:
                if attempt['task'] == 'study_coverage' and attempt['status'] == 'started':
                    attempt.update(status='uncertain', recovery_stop='interrupted_outcome_unknown')
                    session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
            from projectclarity.providers.model_retry import save_model_attempts
            save_model_attempts(session, output_root)
            save_study(session, output_root)
        if active:
            task = session.workflow['tasks'][active]
            task.update(status='uncertain', error_code='interrupted_outcome_unknown')
            session.workflow['active'] = None
            # Conservatively charge time since the last durable acquisition clock;
            # downtime cannot silently replenish an interrupted run's allowance.
            saved_at = session.retrieval_usage.get('checkpoint_wall_time', task.get('started_at', time.time()))
            if session.retrieval_usage:
                session.retrieval_usage['elapsed_seconds'] += max(0, time.time() - saved_at)
            for passage in (session.extraction or {}).get('attempts', []):
                if passage['status'] == 'started':
                    passage.update(status='failed', error_code='interrupted_outcome_unknown')
        for attempt in session.model_attempts:
            if attempt['status'] == 'started':
                attempt.update(status='uncertain', recovery_stop='interrupted_outcome_unknown')
                session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
        from projectclarity.providers.model_retry import save_model_attempts
        save_model_attempts(session, output_root)
        from projectclarity.claim_review.review_queue import recover_review_batches
        recover_review_batches(session)
        if session.workflow.get('review_batches'):
            write_text(directory / 'claim_reviews.json',
                json_text({'schema_version': '1.0', 'reviews': session.claim_reviews}))
        session.workflow.setdefault('resumes', []).append(datetime.now(UTC).isoformat())
        save_session(session, output_root)
        monitor.resume(stage='schedule_work', run_status=session.status)
        with monitor.termination_handler(), tracing_context(enabled=False), checkpoint_store(output_root, create=True) as store:
            graph = build_graph(output_root, key_reader, monitor, checkpointer=store)
            result = graph.invoke({'request': session.request.model_dump(mode='json'),
                'session': session.model_dump(mode='json'), 'run_id': run_id},
                thread_config(run_id), durability="exit")
            return finish_graph_invocation(result, output_root, monitor, graph)
    finally:
        monitor.close()
