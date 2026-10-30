"""Run permitted search queries and retain every query's outcome and result references.

Initial queries come from the research plan; later suggestions can address gaps
identified in the collected evidence. This step applies request and source limits,
uses the shared search executor for provider recovery, normalizes returned URLs
and records failures separately from genuinely empty results. A later selection
step decides which results become sources. Search snippets themselves never serve
as quotations supporting extracted claims.
"""
import json
import time
from itertools import zip_longest

from projectclarity.discovery.observed_link_inventory import canonical_url
from projectclarity.discovery.search_result_urls import normalize_search_result_url
from projectclarity.discovery.search_provider import SearchProviderError, search_web
from projectclarity.discovery.search_execution import execute_search
from projectclarity.discovery.search_engines import engine_order, search_readiness
from projectclarity.discovery.url_policy import url_denial
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import save_session, write_text


def save_search(session, output_root):
    """Retain the cumulative query/result/selection ledger separately from content-free events.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        None. Write the cumulative search ledger and updated session to the run directory.
    """
    write_text(output_root / session.session_id / "search.json", json.dumps(session.search, indent=2) + "\n")
    save_session(session, output_root)


def proposed_queries(session):
    """Choose initial planner queries or later evidence-guided proposals without inventing
    queries.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        During the preliminary study, only its validated, unattempted queries.
        Before any main-research query is attempted, planner proposals first, preserving their
        strategy/within-step order for the broad overview round. Later, under
        exploration-cycle scheduling, fresh coverage proposals first, then
        deferred failed queries and unused strategy queries. Legacy runs interleave
        coverage/strategy after deferred queries. Duplicate normalized proposals
        are removed; successful queries never repeat. At the logical-query ceiling,
        prioritize remaining deferred receipts exclusively so new proposals cannot
        hide retry identity. Designed-report blocker queries precede unused planner
        queries even before the initial main-research round; otherwise a scheduler
        handoff could execute unrelated broad searches. Without retries, retain
        blocked proposals for reporting.
        Study queries retain separate purpose labels and do not suppress that broad
        initial main-research round. No query text or topic-specific ordering is synthesized here.
    """
    attempted = {q['query'].casefold() for q in session.search.get('queries', [])}
    groups = [[{"query": q, "goal_ids": step.goal_ids, "reason": step.action}
        for q in step.proposed_searches] for step in session.plan.strategy]
    initial = not any(q.get('purpose') != 'coverage_study' for q in session.search.get('queries', []))
    if session.coverage_study.get('status') == 'running' and session.coverage_study.get('phase') == 'search':
        return [q for q in session.coverage_study['plan']['queries']
            if ' '.join(q['query'].split()).casefold() not in attempted]
    planned = [q for row in (groups if initial else zip_longest(*groups)) for q in row
        if q is not None and ' '.join(q['query'].split()).casefold() not in attempted]
    coverage = [q for q in session.coverage.get('next_searches', [])
        if ' '.join(q['query'].split()).casefold() not in attempted]
    goals = {goal.goal_id for goal in session.plan.goals}
    deferred = [q for q in session.search.get('queries', []) if q['status'] == 'deferred']
    cap = session.request.max_search_queries
    if deferred and cap is not None and len(session.search.get('queries', [])) >= cap:
        return deferred
    ordered = (planned + coverage + deferred if initial else
        coverage + deferred + planned if session.request.explore_before_search_depth and session.request.follow_links
        else deferred + [q for row in zip_longest(coverage, planned) for q in row if q is not None])
    if session.answer_schema and coverage:
        ordered = coverage + deferred + planned
    unique = {}
    for query in ordered:
        key = ' '.join(query['query'].split()).casefold()
        if 0 < len(key) <= 300 and not set(query['goal_ids']) - goals:
            unique.setdefault(key, query)
    return list(unique.values())


def pending_search_results(session):
    """Return unconsidered or capacity-deferred results so later rounds do not lose leads.

    Args:
        session: ResearchSession with the retained search inventory and selection receipts.

    Returns:
        Eligible result IDs not yet offered, plus valid selections deferred by capacity.
        Declined items close; unusable replies allow one bounded recovery attempt.
    """
    search = session.search
    from projectclarity.discovery.selection_acceptance import pending_offered_ids
    registered = {canonical_url(s.url) for s in session.sources if s.kind == 'url'}
    rows = [r for r in search.get('results', []) if r['eligible'] and r['url'] not in registered]
    return pending_offered_ids(rows, search.get('selections', []),
        'offered_ids', 'deferred_ids', 'error_code')


def search_can_continue(session):
    """Check permissions, hard ceilings and previous failures without interpreting completion
    as budget.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        True when permissions and remaining limits allow pending query work, possibly
        after a provider cooldown; False for a terminal operational stop. The scheduler
        separately checks absolute readiness. Failed logical queries consume query
        slots; bounded deferred rounds reuse their slot and preserve engine attempts.
        Exhausted engines cannot retry. Legacy provider breakers and model-provider
        outages remain terminal; malformed selections do not block independent work.
    """
    return search_stop_reason(session) is None


def search_source_limit(session):
    """Resolve an explicit search-source sublimit or share the overall source allowance.

    Args:
        session: ResearchSession containing immutable request limits.

    Returns:
        Effective cumulative search-selected-source ceiling. None in the request means
        no separate partition, not unlimited sources or a change to saved old limits.
    """
    limit = session.request.max_search_sources
    return session.request.max_sources if limit is None else limit


def search_stop_reason(session):
    """Explain an operational stop independently of any coverage judgment.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        An application-owned terminal stop reason, or None when query work remains
        authorized. A cooldown is not terminal: search_readiness supplies its future
        deadline and the scheduler must wait before invoking the node.
    """
    search = session.search
    if not session.request.web_search:
        return "search_not_authorized"
    _, terminal = search_readiness(session)
    if terminal:
        return terminal
    if session.status == "cancelled":
        return "cancelled"
    if search.get('stop_reason') == 'fetch_budget_exhausted':
        return 'fetch_budget_exhausted'
    if any(c.get("error_code") == "provider_or_adapter_error" for c in session.processing_calls):
        return "model_provider_unavailable"
    if session.request.max_search_queries is not None and len(search.get("queries", [])) >= session.request.max_search_queries and not any(
            q['status'] == 'deferred' for q in search.get('queries', [])):
        return "search_query_limit"
    if len(search.get("selected_sources", [])) >= search_source_limit(session) or len(session.sources) >= session.request.max_sources:
        return "search_source_limit"
    if session.usage.requests_attempted >= session.request.budget.max_requests:
        return "model_request_limit"
    if not proposed_queries(session):
        return "no_new_queries_proposed"
    return None


def web_search(state, output_root, monitor=None, web=None, *, record_visit=True):
    """Execute authorized, bounded search queries to discover source leads for the current
    research goals.

    Reserve each logical query before invoking engines so failures consume its
    allowance. Deferred rounds reuse that receipt and retain all engine attempts;
    their separate recovery bound prevents unlimited replay. Keep result provenance,
    exclusions and stop reasons in a cumulative ledger. Retain original URLs,
    exclude known advertising-click endpoints, and decode recognized organic
    wrappers locally before destination policy and deduplication checks.
    This node neither downloads result pages nor extracts facts from snippets.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.
        web: Optional shared retrieval runtime; its elapsed time and acquisition budgets
            also prevent new search attempts after retrieval is exhausted.
        record_visit: False when the early-study node reuses this executor internally;
            do not invent a separate graph-node visit for nested work.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    if record_visit:
        session.visited_nodes.append("web_search")
    proposals = proposed_queries(session)
    search = session.search
    search.setdefault("schema_version", "1.0")
    search.setdefault("provider", "ddgs_" + session.request.search_provider)
    search.setdefault('provider_order', engine_order(session.request))
    search.setdefault("queries", [])
    search.setdefault("results", [])
    search.setdefault("selected_sources", [])
    search.setdefault("selections", [])
    search["active_result_ids"] = []
    if not session.request.web_search or session.request.plan_only:
        search["stop_reason"] = "search_not_authorized"
        save_search(session, output_root)
        return {"session": session}
    if not search_can_continue(session):
        search["stop_reason"] = search_stop_reason(session) or "search_limit_or_no_queries"
        save_search(session, output_root)
        return {"session": session}
    search["round"] = search.get("round", 0) + 1
    # Up to two queries per round, sharing the run's cumulative ceiling.
    cap = session.request.max_search_queries
    remaining = 2
    known_goals = {g.goal_id for g in session.plan.goals}
    seen_queries = {q["query"].casefold() for q in search["queries"]}
    seen_urls = {canonical_url(s.url) for s in session.sources if s.kind == "url"}
    seen_urls.update(r["url"] for r in search["results"] if r["url"])
    search["last_proposals"] = proposals
    search["stop_reason"] = None
    for proposal in proposals:
        ready_at, terminal = search_readiness(session)
        if terminal or ready_at > time.time():
            search['stop_reason'] = terminal or 'search_cooldown'
            break
        if web is not None and (time.monotonic() - web.budget.started >= web.budget.limits.max_run_seconds
                or web.budget.http_requests >= web.budget.limits.max_http_requests
                or web.budget.bytes_read >= web.budget.limits.max_total_bytes):
            search['stop_reason'] = 'fetch_budget_exhausted'
            break
        query = " ".join(proposal["query"].split())
        if not query or len(query) > 300 or set(proposal["goal_ids"]) - known_goals:
            continue
        retry = proposal.get('status') == 'deferred'
        if (query.casefold() in seen_queries and not retry) or remaining == 0:
            continue
        if not retry and cap is not None and len(search['queries']) >= cap:
            continue
        seen_queries.add(query.casefold())
        remaining -= 1
        receipt = proposal if retry else {"query_id": f"Q{len(search['queries']) + 1}", "round": search["round"],
            "query": query, "goal_ids": proposal["goal_ids"], "reason": proposal["reason"],
            "status": "started", "error_code": None, "provider": session.request.search_provider}
        if not retry:
            if session.coverage_study.get('phase') == 'search' and session.coverage_study.get('status') == 'running':
                receipt['purpose'] = 'coverage_study'
            search["queries"].append(receipt)
        receipt.update(status='started', error_code=None, rounds=receipt.get('rounds', 0) + 1,
            execution_round=search['round'])
        save_search(session, output_root)
        if monitor:
            monitor.observe('search_query_started', query_id=receipt['query_id'], search_provider=receipt['provider'])
        try:
            if monitor:
                monitor.check_cancel()
            results = execute_search(session, receipt, search_web, monitor, web,
                persist=lambda: save_search(session, output_root))
            if monitor:
                monitor.check_cancel()
            for result in results[:session.request.search_results_per_query]:
                if not isinstance(result, dict):
                    raise SearchProviderError("invalid_search_response")
                original_url = str(result.get("url", ""))
                url, exclusion = normalize_search_result_url(original_url)
                if exclusion is None:
                    exclusion = url_denial(url, session.request.url_policy) or ("duplicate_url" if url in seen_urls else None)
                item = {"result_id": f"R{len(search['results']) + 1}", "query_id": receipt["query_id"],
                    "provider": receipt['provider'],
                    "round": search["round"], "url": url, "original_url": original_url[:2048],
                    "title": str(result.get("title", ""))[:300], "snippet": str(result.get("snippet", ""))[:700],
                    "eligible": exclusion is None, "exclusion_reason": exclusion}
                search["results"].append(item)
                if url:
                    seen_urls.add(url)
                if exclusion is None:
                    search["active_result_ids"].append(item["result_id"])
            receipt["status"] = "completed"
            receipt.pop('ready_at', None)
            receipt['result_count'] = len(results[:session.request.search_results_per_query])
        except (RunCancelled, KeyboardInterrupt, EOFError):
            receipt.update(status="failed", error_code="cancelled")
            search["stop_reason"] = "cancelled"
            session.status = "cancelled"
            break
        except SearchProviderError as exc:
            receipt.update(status="failed", error_code=str(exc), diagnostics=exc.diagnostics)
            ready_at, terminal = search_readiness(session)
            if not terminal and str(exc) != 'fetch_budget_exhausted' and receipt['rounds'] <= session.request.search_recovery_rounds:
                receipt.update(status='deferred', ready_at=ready_at)
            search["stop_reason"] = str(exc)
        finally:
            if web is not None:
                session.retrieval_usage = web.budget.summary()
            save_search(session, output_root)
            if monitor:
                monitor.observe('search_query_finished', query_id=receipt['query_id'], search_provider=receipt['provider'],
                    result_count=receipt.get('result_count', 0), call_status=receipt['status'], error_code=receipt.get('error_code'))
    search['active_result_ids'] = pending_search_results(session)
    search["stop_reason"] = search["stop_reason"] or ("results_ready" if search["active_result_ids"] else "no_new_results")
    save_search(session, output_root)
    return {"session": session}
