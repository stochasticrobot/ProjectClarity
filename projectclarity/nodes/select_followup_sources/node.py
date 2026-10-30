"""Choose additional sources from hyperlinks found in pages already acquired.

This step offers the model a size-limited selection of observed URLs and labels,
research goals and recorded findings. Python checks proposed IDs, goal links and
capacity before registering destinations. For example, a university directory can
lead to a staff profile even if the directory yielded no answer claims. The model
cannot authorize a URL absent from the offered link inventory.

Unconsidered links can remain available for later turns. Decisions and source
ancestry are saved in discovery.json; the scheduler chooses subsequent retrieval
or other work. URL restrictions still apply, including optional same-origin or
whitelist-only rules. Successful validation of a choice is not proof of relevance,
complete discovery or source truth.
"""
import json
import time
from collections import Counter

from projectclarity.discovery.followup_selection_contracts import FollowupPlan, ContractFollowupPlan
from projectclarity.discovery.branch_progress import branch_progress
from projectclarity.discovery.observed_link_inventory import inventory_links, seed_origins, discovery_connections
from projectclarity.discovery.inventory_context import pack_inventory, payload_messages
from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.prompt_context import research_context
from projectclarity.research_schemas import URLSource
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import save_session, write_text

from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('select_followup_sources.txt')


def save_discovery(session, output_root):
    """Persist decisions separately from operational events, which omit research content.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        None. Write the private discovery decisions and updated session to the run
        directory.
    """
    write_text(output_root / session.session_id / "discovery.json",
        json.dumps(session.discovery, indent=2) + "\n")
    save_session(session, output_root)


def select_followup_sources(state, output_root, key_reader=read_api_key, monitor=None, web=None):
    """Expand the observed-link frontier so research can follow threads and retain intersections.

    Check explicit follow-links permission and remaining source, retrieval and model-call
    limits before asking the model to rank observed links. Validate the returned link IDs,
    register eligible selections up to capacity, retain surplus proposals and unmentioned
    offers after a full batch in the frontier (a non-full reply can decline unused offers),
    and save the selection or the reason
    expansion stopped. This node selects sources; the retrieval node downloads them. Versioned answer
    runs require explicit dispositions and requirement links at the shared boundary.
    Offer complete claim summaries and paired human guidance within the context budget;
    link depth and deterministic branch progress stay in eligibility checks, not the
    model's ranking payload. Save paused/pruned parent decisions even without a call.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        key_reader: Zero-argument callable returning the model API key when a request needs
            it; defaults to read_api_key.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.
        web: Optional invocation-shared PublicWebReader whose consumed time, byte and
            request budgets constrain expansion. None means no shared reader is available;
            this node does not fetch pages.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    session.visited_nodes.append("select_followup_sources")
    if web is not None:
        session.retrieval_usage = web.budget.summary()
    previous = session.discovery
    progress = branch_progress(session)
    history = list(previous.get('history', []))
    if previous.get('selection_attempted'):
        history.append({key: previous.get(key) for key in
            ('round', 'call_id', 'proposal', 'stop_reason', 'registered_this_round', 'offered_link_ids',
             'offered_claim_ids', 'deferred_link_ids', 'capacity_deferred_ids', 'context_inventory', 'evidence_epoch',
             'items', 'accepted_ids')})
    discovery = {"schema_version": "2.0", "hop": previous.get('hop', 0),
        "round": previous.get('round', 0) + 1, "history": history, "selection_attempted": False,
        "allowed_origins": sorted(seed_origins(session)) if session.request.same_origin_links else None,
        "links": inventory_links(session, progress), "branch_progress": progress, "registered_this_round": [],
        "parent_inventories_truncated": [d.source_id for d in session.documents if d.links_truncated],
        "selected_sources": list(previous.get('selected_sources', [])), "stop_reason": None}

    session.discovery = discovery
    discovery = session.discovery  # Pydantic validates/copies assigned dictionaries.
    discovery['connections'] = discovery_connections(session, discovery['links'])
    from projectclarity.discovery.selection_acceptance import pending_offered_ids
    eligible = [link for link in discovery['links'] if link['eligible']]
    available_count = len(eligible)
    pending = pending_offered_ids(eligible, history, 'offered_link_ids', 'capacity_deferred_ids', 'stop_reason')
    eligible = [link for link in eligible if link['link_id'] in pending]
    discovery['offered_link_ids'] = []
    # Leave at least one extraction and review call after selection. This is a
    # minimum allowance, not a guarantee: a long page may contain many passages.
    from projectclarity.nodes.schedule_work.workflow_control import processing_reserve
    remaining = session.request.budget.max_requests - session.usage.requests_attempted - processing_reserve(session)
    capacity = min(session.request.max_followup_sources, session.request.max_sources - len(session.sources), max(0, (remaining - 1) // 2))
    if not session.request.follow_links:
        discovery["stop_reason"] = "supplied_sources_only"
    elif session.status == "cancelled":
        discovery["stop_reason"] = "cancelled"
    elif any(call.get("error_code") == "provider_or_adapter_error" for call in session.processing_calls) or any(
            attempt.get("error_code") == "provider_or_adapter_error" for attempt in (session.extraction or {}).get("attempts", [])):
        discovery["stop_reason"] = "provider_unavailable"
    elif not eligible:
        if available_count:
            discovery['stop_reason'] = ('frontier_selection_exhausted' if any(h.get('stop_reason') in
                {'invalid_selection_references', 'invalid_output_schema', 'incomplete_model_output'} for h in history)
                else 'frontier_considered')
        else:
            reasons = {l['exclusion_reason'] for l in discovery['links']}
            discovery['stop_reason'] = next((r for r in ('branch_analysis_pending', 'branch_path_search_limit', 'branch_path_unavailable',
                'branch_unproductive_limit', 'search_exploration_boundary', 'link_depth_limit') if r in reasons), 'no_eligible_links')
    elif remaining < 3 or capacity == 0:
        discovery["stop_reason"] = "request_or_source_limit"
    elif web and (time.monotonic() - web.budget.started >= web.budget.limits.max_run_seconds
            or web.budget.http_requests >= web.budget.limits.max_http_requests
            or web.budget.bytes_read >= web.budget.limits.max_total_bytes):
        discovery["stop_reason"] = "fetch_budget_exhausted"
    if discovery["stop_reason"]:
        save_discovery(session, output_root)
        if monitor:
            monitor.observe("discovery_stopped", stage="discovery", error_code=discovery["stop_reason"])
        return {"session": session}
    record = ExtractionRecord.model_validate(session.extraction) if session.extraction else ExtractionRecord()
    # Claims receive one quarter of context; the rest stays available for the
    # goal-aware link selector. Both inventories rotate rather than taking prefixes.
    summaries = [{"candidate_id": c.candidate_id, "source_id": c.source_id, "statement": c.proposal.statement,
        "goal_ids": c.proposal.goal_ids, "status": c.adjudication_status,
        "reason": c.adjudication_reason or ""} for c in record.candidates]
    claim_exposures = Counter(cid for h in history for cid in (h.get('offered_claim_ids') or []))
    claim_context, deferred_claims = pack_inventory("", {}, "claims", summaries,
        id_key="candidate_id", source_key="source_id", exposures=claim_exposures, max_characters=12_000)
    summaries = claim_context["claims"]
    discovery['offered_claim_ids'] = [c['candidate_id'] for c in summaries]
    payload = {"original_question": session.request.question,
        "goals": [goal.model_dump(mode="json") for goal in session.plan.goals],
        "scope_in": session.plan.scope_in, "scope_out": session.plan.scope_out,
        "completion_criteria": [c.model_dump(mode="json") for c in session.plan.completion_criteria],
        "coverage_progress": [{k: c.get(k) for k in ('criterion_id', 'status', 'observed_count', 'gaps')}
            for c in session.coverage.get("criteria_progress", [])],
        "scope_estimates": session.coverage.get("scope_estimates",
            [e.model_dump(mode='json') for e in session.plan.scope_estimates]),
        **research_context(session),
        "claim_summaries": summaries, "claim_summary_total": len(record.candidates),
        "claim_summaries_truncated": bool(deferred_claims),
        "extraction_incomplete": any(a.status != "completed" for a in record.attempts) or bool(record.source_errors),
        "max_selections": capacity}
    payload['registered_sources'] = [{'source_id': source.source_id, 'url': source.url}
        for source in session.sources if source.kind == 'url']
    from projectclarity.coverage.goal_register import goal_progress_summary
    payload['cumulative_goal_progress'] = goal_progress_summary(session)
    exposures = Counter(lid for h in history for lid in (h.get('offered_link_ids') or []))
    rows = [{"link_id": l["link_id"], "label": l["label"], "url": l["url"],
             "parent_source_id": l["parent_source_id"]} for l in eligible]
    try:
        payload, deferred = pack_inventory(SYSTEM_PROMPT, payload, "links", rows,
            id_key="link_id", source_key="parent_source_id", exposures=exposures)
    except ValueError:
        discovery['stop_reason'] = 'frontier_context_blocked'
        save_discovery(session, output_root)
        return {"session": session}
    discovery.update(offered_link_ids=[l['link_id'] for l in payload['links']],
        deferred_link_ids=deferred, context_inventory=payload['context_inventory'],
        evidence_epoch=[len(session.documents), len(record.candidates)])
    if not payload['links']:
        discovery['stop_reason'] = 'frontier_context_blocked'
        save_discovery(session, output_root)
        return {"session": session}
    discovery["selection_attempted"] = True
    save_discovery(session, output_root)
    from projectclarity.nodes.schedule_work.answer_controller import enabled
    controlled = enabled(session)
    answer, receipt = invoke_structured(session, output_root, call_id=f"discovery_hop{discovery['round']}",
        task="select_followup_sources", prompt_version="followup-dispositions-v1" if controlled else "followup-selection-v6",
        messages=payload_messages(SYSTEM_PROMPT, payload),
        schema=ContractFollowupPlan if controlled else FollowupPlan, key_reader=key_reader, monitor=monitor)
    discovery["call_id"] = receipt["call_id"]
    if answer is None:
        discovery["stop_reason"] = receipt["error_code"] or "selection_failed"
        if session.status != "cancelled":
            session.status = "extraction_partial"
            session.error = "Follow-up selection did not finish; inspect discovery.json and llm_calls."
    else:
        offered = {link["link_id"]: link for link in eligible if link['link_id'] in discovery['offered_link_ids']}
        from projectclarity.discovery.selection_acceptance import accept_choices
        discovery['proposal'] = answer.model_dump(mode='json')
        accepted, outcomes = accept_choices(session, answer.selections, offered, 'link_id', capacity)
        discovery['items'] = outcomes
        discovery['accepted_ids'] = [c.link_id for c in accepted]
        discovery['capacity_deferred_ids'] = [i['link_id'] for i in outcomes if i['status'] == 'deferred']
        # A full batch is not a rejection of every other offered destination.
        # Retain unmentioned leads for later rounds; explicit invalid proposals
        # remain rejected. A non-full valid reply can close its unused offers.
        if len(accepted) == capacity:
            mentioned = {i['link_id'] for i in outcomes}
            discovery['capacity_deferred_ids'].extend(
                identifier for identifier in discovery['offered_link_ids'] if identifier not in mentioned)
        try:
            if monitor:
                monitor.check_cancel()
            for choice in accepted:
                link = offered[choice.link_id]
                source = URLSource(source_id=f"S{len(session.sources) + 1}", url=link['url'],
                    discovered_from_link=choice.link_id, depth=link['depth'])
                session.sources.append(source)
                discovery['selected_sources'].append({'source_id': source.source_id, **choice.model_dump(mode='json')})
                discovery['registered_this_round'].append(source.source_id)
                discovery['hop'] = max(discovery['hop'], source.depth)
            discovery['connections'] = discovery_connections(session, discovery['links'])
            rejected = any(i['status'] == 'rejected' for i in outcomes)
            discovery['stop_reason'] = ('invalid_selection_references' if rejected else
                'followup_registered' if accepted else 'no_useful_links_selected')
            remaining_ids = pending_offered_ids(eligible, [*history, discovery],
                'offered_link_ids', 'capacity_deferred_ids', 'stop_reason')
            if not accepted and not rejected and remaining_ids:
                discovery['stop_reason'] = 'frontier_batch_deferred'
            if monitor:
                monitor.register_sources(session.sources)
        except (RunCancelled, KeyboardInterrupt, EOFError):
            session.status = 'cancelled'
            discovery['stop_reason'] = 'cancelled'
    save_discovery(session, output_root)
    if monitor:
        monitor.observe("discovery_finished", stage="discovery", selected_sources=len(discovery["registered_this_round"]),
            eligible_links=len(eligible), error_code=discovery["stop_reason"])
    return {"session": session}
