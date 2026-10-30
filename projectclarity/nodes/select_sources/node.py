"""Ask the model which saved search results should become sources to read.

The model sees the research goals and an allowed set of search-result IDs. It can
propose direct evidence, a useful directory, background context or a specific lead.
Python checks each proposal and registers only the matching observed URL, within
source capacity and access rules. Selection does not download the page or verify
its claims; retrieval, extraction and review follow as separate scheduled steps.
"""
import json
from pydantic import Field

from projectclarity.nodes.web_search.node import save_search, search_source_limit, pending_search_results, search_can_continue
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.prompt_context import research_context
from projectclarity.research_schemas import Contract, Identifier, Text, URLSource


class SearchSourceChoice(Contract):
    """A ranked result ID, its research purpose and goals; no model-authored URL."""

    result_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    reason: Text


class SearchSourceSelection(Contract):
    """Ordered useful leads; an empty selection is a legitimate result."""

    selections: list[SearchSourceChoice]


from projectclarity.discovery.source_disposition import SourceDisposition


class ContractSearchChoice(SearchSourceChoice, SourceDisposition):
    """An offered search result with an explicit requirement-linked disposition."""


class ContractSearchSelection(Contract):
    """Ordered source decisions; rejected entries never authorize acquisition."""

    selections: list[ContractSearchChoice]


from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('select_search_sources.txt')


def select_sources(state, output_root, key_reader=read_api_key, monitor=None):
    """Turn ranked web-search results into registered sources so the normal retrieval and
    evidence pipeline can process them.

    Ask the model to select from the observed result inventory, validate returned IDs and
    goal references, then apply source limits and duplicate checks. Retain valid surplus
    rankings for later selection rather than rejecting the reply for exceeding capacity.
    Capacity also leaves a minimum extraction/review opportunity per accepted source
    within the shared request budget, excluding the final coverage reservation.
    Registered-source summaries include bounded
    run-local result examples and their statuses, never unseen destination contents.
    New answer-controller runs require explicit dispositions and requirement links;
    rejection cannot authorize acquisition. Search snippets remain
    leads rather than claim evidence; only later retrieval supplies source text. Offer
    result IDs, URLs, titles and snippets with interpreted human guidance, not internal
    eligibility receipts or query IDs whose queries are absent.

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
    session.visited_nodes.append("select_sources")
    search = session.search
    search.setdefault("selections", [])
    search['active_result_ids'] = pending_search_results(session)
    offered = {r["result_id"]: r for r in search.get("results", [])
        if r["result_id"] in search.get("active_result_ids", []) and r["eligible"]}
    # Ten is a processing batch, not a validation rule or cumulative quota.
    capacity = min(10, search_source_limit(session) - len(search.get("selected_sources", [])), session.request.max_sources - len(session.sources))
    if session.request.extract_knowledge:
        from projectclarity.nodes.schedule_work.workflow_control import processing_reserve
        from projectclarity.coverage.checkpoints import coverage_enabled
        remaining = (session.request.budget.max_requests - session.usage.requests_attempted
                     - int(coverage_enabled(session)))
        capacity = min(capacity, max(0, (remaining - 1 - session.request.model_retries - processing_reserve(session))
            // (1 + int(session.request.review_claims))))
    search["registered_this_round"] = []
    if not session.request.web_search or not offered or capacity <= 0 or session.status == "cancelled":
        if session.request.web_search and not session.sources and session.status != 'cancelled' and not search_can_continue(session):
            session.status = 'retrieval_failed'
            session.error = 'No source was available for retrieval before discovery stopped; inspect search.json.'
        save_search(session, output_root)
        return {"session": session}
    from projectclarity.nodes.schedule_work.answer_controller import enabled
    controlled = enabled(session)
    payload = {"original_question": session.request.question,
        "goals": [g.model_dump(mode="json") for g in session.plan.goals],
        "criteria": [c.model_dump(mode="json") for c in session.plan.completion_criteria],
        "scope_in": session.plan.scope_in, "scope_out": session.plan.scope_out,
        **research_context(session),
        "coverage": [{k: c.get(k) for k in ('criterion_id', 'status', 'observed_count', 'gaps')}
            for c in session.coverage.get("criteria_progress", [])],
        "scope_estimates": session.coverage.get("scope_estimates",
            [e.model_dump(mode='json') for e in session.plan.scope_estimates]),
        "results": [{k: r[k] for k in ('result_id', 'url', 'title', 'snippet')}
            for r in offered.values()], "capacity": capacity}
    from projectclarity.coverage.goal_register import goal_progress_summary
    payload['cumulative_goal_progress'] = goal_progress_summary(session)
    from projectclarity.discovery.registered_source_context import add_registered_context
    add_registered_context(session, payload, SYSTEM_PROMPT)
    answer, receipt = invoke_structured(session, output_root, call_id=f"search_select_{len(search['selections']) + 1}",
        task="select_sources", prompt_version="search-source-dispositions-v1" if controlled else "search-source-selection-v4",
        messages=[("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))],
        schema=ContractSearchSelection if controlled else SearchSourceSelection, key_reader=key_reader, monitor=monitor)
    selection = {"round": search["round"], "call_id": receipt["call_id"],
        "offered_ids": list(offered), "capacity": capacity, "proposal": None, "error_code": None}
    search["selections"].append(selection)
    if answer is None:
        selection["error_code"] = receipt["error_code"]
    else:
        selection["proposal"] = answer.model_dump(mode="json")
        from projectclarity.discovery.selection_acceptance import accept_choices
        accepted, outcomes = accept_choices(session, answer.selections, offered, 'result_id', capacity)
        selection['items'] = outcomes
        selection['accepted_ids'] = [c.result_id for c in accepted]
        selection['deferred_ids'] = [i['result_id'] for i in outcomes if i['status'] == 'deferred']
        if any(i['status'] == 'rejected' for i in outcomes):
            selection['error_code'] = 'invalid_search_selection'
        for choice in accepted:
            result = offered[choice.result_id]
            source = URLSource(source_id=f"S{len(session.sources) + 1}", url=result['url'],
                discovered_from_search=result['result_id'])
            session.sources.append(source)
            search['selected_sources'].append({'source_id': source.source_id, **choice.model_dump(mode='json')})
            search['registered_this_round'].append(source.source_id)
        if monitor:
            monitor.register_sources(session.sources)
    search["stop_reason"] = selection["error_code"] or ("sources_registered" if search["registered_this_round"] else "no_useful_results_selected")
    save_search(session, output_root)
    return {"session": session}
