"""Read the sources chosen by the scheduler and save their acquisition outcomes.

A source is a registered web URL or local document path. This workflow step obtains
the shared retrieval limits and calls the source reader to save original bytes,
parsed text and metadata. It records failures as well as successful reads so later
steps can distinguish an unread document from a document containing no findings.
It does not choose new sources or ask a model to extract claims.
"""
from pathlib import Path
from projectclarity.workflow_state import ResearchState
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.retrieval.source_retrieval import retrieve_one, save_manifest
from projectclarity.discovery.observed_link_inventory import seed_origins
from projectclarity.knowledge.source_value import record_source_values
from projectclarity.plan_renderer import render_plan
from projectclarity.session_storage import save_session, write_text


def retrieve_sources(state: ResearchState, output_root: Path, monitor=None, runtime=None, *, only_source_ids=None, record_visit=True) -> dict:
    """Retrieve registered sources that do not yet have receipts, preserving each outcome for
    later extraction.

    With an active scheduler reservation this attempts only its selected source;
    direct callers without a journal retain the batch interface. Retry directories
    preserve prior receipts, while the document index keeps each latest outcome.
    Registered sources include supplied inputs, authorized web-search selections and
    authorized observed links. Search suggestions alone never grant permission to fetch a
    page. Each source attempt saves its receipt before the next begins, so partial results
    remain inspectable.

    One invocation shares a web reader across rounds, including byte/request/time counters,
    application-owned per-origin pacing. Runtime clients remain outside durable
    checkpoints.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.
        runtime: Mutable invocation-local dictionary holding the shared PublicWebReader;
            reused across retrieval rounds but never serialized into graph state.
        only_source_ids: Optional bounded source-ID collection for an early coverage probe;
            None retains normal scheduled/batch retrieval. Already acquired pages are reused.
        record_visit: False for internal executor reuse by the early-study graph node;
            no separate graph-node visit is then recorded.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    if record_visit:
        session.visited_nodes.append("retrieve_sources")
    session.status = "retrieving"
    directory = output_root / session.session_id
    runtime = runtime if runtime is not None else {}
    web = retrieval_runtime(session, runtime, monitor)
    budget = web.budget
    attempted_ids = {document.source_id for document in session.documents}
    active = session.workflow.get('tasks', {}).get(session.workflow.get('active'), {})
    scheduled_source = active.get('source_id') if active.get('action') == 'retrieve_sources' else None
    save_session(session, output_root)
    try:
        if monitor:
            monitor.observe("retrieval_started", stage="retrieving", run_status="retrieving")
        for source in session.sources:
            if only_source_ids is not None and source.source_id not in only_source_ids:
                continue
            if scheduled_source:
                if source.source_id != scheduled_source:
                    continue
            elif source.source_id in attempted_ids:
                continue
            # Domain policy applies to every URL and redirect; seed-origin confinement
            # is a separate optional restriction on followed links of any depth.
            web.url_policy = session.request.url_policy
            web.allowed_origins = seed_origins(session) if session.request.same_origin_links and getattr(source, "depth", 0) > 0 else None
            if monitor:
                monitor.check_cancel()
                monitor.observe("source_started", source_id=source.source_id, stage="acquiring")
            attempt_number = active.get('attempts', 1)
            record = (retrieve_one(source, directory, web, attempt_number=attempt_number)
                if attempt_number > 1 else retrieve_one(source, directory, web))
            if scheduled_source:
                session.workflow.setdefault('retrieval_attempts', []).append(record.model_dump(mode='json'))
            session.documents = [d for d in session.documents if d.source_id != source.source_id] + [record]
            session.workflow['blocked_origins'] = sorted(web.blocked_origins)
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
    record_source_values(session, output_root)
    save_session(session, output_root)
    write_text(directory / "plan.md", render_plan(session))
    return {"session": session}

