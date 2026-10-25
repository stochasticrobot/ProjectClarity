"""Persist bounded crawl waves between search batches without adding model tasks.

The existing selector still chooses links and the existing coverage task proposes
queries. This module owns timing only: complete runnable analysis, expand through
the relative horizon, assess gaps, then search. A horizon is not a crawl limit.
It also is not a page/call quota: the whole runnable frontier inside that horizon
has priority, so a wide frontier can exhaust the budget before coverage/search.
The scheduler has no protected search allowance; smaller depth is not a guarantee.
"""


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
        Optional capture is not a prerequisite for search, but runnable analysis
        and link expansion take priority; their breadth can consume all model calls.
    """
    cycle = session.workflow.get('exploration_cycle')
    if not (session.request.explore_before_search_depth and cycle):
        return available
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
