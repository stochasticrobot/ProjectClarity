"""Limit how much link-following work runs before another search opportunity.

For the older scheduling policy, an exploration cycle is the work done between
search batches. These helpers remember the cycle's starting link depth and model
usage, then select eligible work until its depth or request checkpoint is reached.
For example, a wide set of linked pages must not indefinitely postpone a queued
search. The saved counters survive restart. This module selects work and updates
in-memory bookkeeping; it does not make searches, retrieve pages or call a model.
"""

MAX_EXPLORE_MODEL_CALLS = 12


def exploration_budget_due(session):
    """Recognize a crawl checkpoint before frontier breadth consumes all calls.

    Args:
        session: Run with current exploration cycle and cumulative request usage.

    Returns:
        True after twelve calls in this wave or with four requests left. Disabled
        exploration returns False. Review flushing can still consume the remaining
        allowance; this is not a guarantee of successful search or new evidence.
    """
    cycle = session.workflow.get('exploration_cycle')
    spent = session.usage.requests_attempted - (cycle or {}).get('start_model_requests', session.usage.requests_attempted)
    return bool(session.request.explore_before_search_depth and session.request.web_search
        and session.request.follow_links and cycle and (
            spent >= MAX_EXPLORE_MODEL_CALLS
            or (spent > 0 and session.request.budget.max_requests - session.usage.requests_attempted <= 4)))


def prepare_cycle(session, reason=None):
    """Initialize, advance or restore the relative-depth exploration journal.

    Args:
        session: Mutable run with saved request policy and registered source ancestry.
        reason: Optional explicit new-cycle reason. None preserves a saved cycle.

    Returns:
        The persisted cycle dictionary, or None when search/link exploration is
        disabled. New cycles anchor all current sources at zero; newly selected
        search results are zero-depth roots, while followed links inherit their
        selected parent's depth plus one. No calls, budget resets or file I/O occur.
    """
    request = session.request
    if not (request.explore_before_search_depth and request.web_search and request.follow_links):
        return None
    cycle = session.workflow.setdefault('exploration_cycle', {})
    if reason or not cycle:
        history = cycle.get('history', [])
        if cycle:
            history.append({k: cycle.get(k) for k in
                ('number', 'depths', 'phase', 'release_reason', 'coverage_status', 'search_round')})
        cycle.update(number=cycle.get('number', 0) + 1, limit=request.explore_before_search_depth,
            start_model_requests=session.usage.requests_attempted,
            depths={s.source_id: 0 for s in session.sources}, phase='explore',
            start_reason=reason or 'initial_sources', release_reason=None,
            coverage_status=None, search_round=session.search.get('round', 0), history=history)
    for source in session.sources:
        if source.source_id not in cycle['depths']:
            parent = (getattr(source, 'discovered_from_link', None) or '').rsplit('_L', 1)[0]
            cycle['depths'][source.source_id] = cycle['depths'].get(parent, -1) + 1
    return cycle


def choose_cycle_work(session, available, delayed):
    """Gate search on analysis and a bounded selected-link frontier, not node visits.

    Args:
        session: Run with current link eligibility and a prepared exploration cycle.
        available: Ready, unblocked journal tasks with attempts remaining.
        delayed: Otherwise runnable tasks waiting on persisted retry deadlines.

    Returns:
        Ready task subset in phase priority, the original list for disabled runs,
        or None after advancing a blocked-search horizon (caller must rebuild the
        inventory once). Failed/uncertain/budget-blocked tasks never masquerade as
        successful analysis. A ready search can proceed when only delayed local
        work remains; cooldown search does not freeze deeper independent crawling.
        Optional capture is not a prerequisite for search. At the call checkpoint,
        pending review precedes gap assessment and search; unfinished crawl work
        stays queued. When search is unavailable, independent crawling continues.
    """
    cycle = session.workflow.get('exploration_cycle')
    if not (session.request.explore_before_search_depth and cycle):
        return available
    cycle.setdefault('start_model_requests', session.usage.requests_attempted)
    if exploration_budget_due(session):
        for actions, phase in (
            ({'review_claims', 'validate_evidence'}, 'flush_reviews'),
            ({'assess_research_gaps'}, 'assess_gaps'),
            ({'select_sources', 'web_search'}, 'search'),
        ):
            ready = [task for task in available if task['action'] in actions]
            if ready:
                cycle.update(phase=phase, release_reason='crawl_call_quantum')
                return ready
    for actions, phase in (
        ({'select_sources'}, 'existing_search_results'),
        ({'retrieve_sources', 'extract_knowledge', 'validate_evidence', 'review_claims'}, 'analyze'),
        ({'select_followup_sources'}, 'explore'),
        ({'assess_research_gaps'}, 'assess_gaps'),
    ):
        ready = [task for task in available if task['action'] in actions]
        if ready:
            cycle['phase'] = phase
            if phase == 'analyze':
                # Keep explicitly authorized auxiliary capture fair alongside
                # analysis, but never make finishing it a prerequisite for search.
                ready += [task for task in available if task['lane'] == 'capture']
            return ready

    from projectclarity.discovery.observed_link_inventory import inventory_links
    boundary = any(link['exclusion_reason'] == 'search_exploration_boundary'
        for link in inventory_links(session))
    unfinished = any(task['status'] in {'blocked', 'failed', 'uncertain'}
        and task['lane'] in {'retrieve', 'process', 'discover'}
        for task in session.workflow.get('tasks', {}).values()) or any(
            task['lane'] in {'retrieve', 'process', 'discover'} for task in delayed)
    cycle['release_reason'] = ('frontier_blocked' if unfinished else
        'relative_horizon_explored' if boundary else 'frontier_exhausted')
    coverage = (session.coverage.get('history') or [{}])[-1]
    cycle['coverage_status'] = coverage.get('status', 'not_available')
    searches = [task for task in available if task['action'] == 'web_search']
    if searches:
        cycle['phase'] = 'search'
        return searches
    if boundary:
        prepare_cycle(session, 'search_unavailable_continue_crawl')
        return None
    cycle['phase'] = 'waiting_or_exhausted'
    return available
