"""Select useful search-result leads before retrieval, with no invented URLs.

This is relevance selection, not claim verification. Direct evidence, indexes,
context, denominator sources and uncertain leads can all be useful. The model
selects IDs; Python registers only URLs from the saved result inventory.
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


from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('select_search_sources.txt')


def select_sources(state, output_root, key_reader=read_api_key, monitor=None):
    """Turn ranked web-search results into registered sources so the normal retrieval and
    evidence pipeline can process them.

    Ask the model to select from the observed result inventory, validate returned IDs and
    goal references, then apply source limits and duplicate checks. Retain valid surplus
    rankings for later selection rather than rejecting the reply for exceeding capacity.
    Search snippets remain
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
    search["registered_this_round"] = []
    if not session.request.web_search or not offered or capacity <= 0 or session.status == "cancelled":
        if session.request.web_search and not session.sources and session.status != 'cancelled' and not search_can_continue(session):
            session.status = 'retrieval_failed'
            session.error = 'No source was available for retrieval before discovery stopped; inspect search.json.'
        save_search(session, output_root)
        return {"session": session}
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
    payload['registered_sources'] = [{'source_id': source.source_id, 'url': source.url}
        for source in session.sources if source.kind == 'url']
    from projectclarity.coverage.goal_register import goal_progress_summary
    payload['cumulative_goal_progress'] = goal_progress_summary(session)
    answer, receipt = invoke_structured(session, output_root, call_id=f"search_select_{len(search['selections']) + 1}",
        task="select_sources", prompt_version="search-source-selection-v4",
        messages=[("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))],
        schema=SearchSourceSelection, key_reader=key_reader, monitor=monitor)
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
