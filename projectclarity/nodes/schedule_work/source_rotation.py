"""Give newly selected sources a first read and pause documents with repeated low yield.

The older scheduling policy uses these helpers to choose eligible reading or search
work. A newly accepted search result should receive processing before another search
replaces it. A focused document that has produced no new records or evidence upgrades
for several completed passages can yield to another source or search. Saved markers
retain that handoff across restarts. No source is fetched and no model is called
here; yielding attention does not declare the document irrelevant or exhausted.
"""
from projectclarity.nodes.schedule_work.source_priority import task_source_priority
from projectclarity.nodes.schedule_work.source_yield import document_yields


def task_source(session, task):
    """Resolve source ownership without inventing a source for global work.

    Args:
        session: Run containing passage inventory.
        task: Ready scheduler task, possibly an inventory-only extraction.
    Returns:
        Source ID, or None for reviews, search and other global tasks.
    """
    return task.get('source_id') or next((a['source_id']
        for a in (session.extraction or {}).get('attempts', [])
        if a['batch_id'] == task.get('batch_id')), None)


def selected_source_work(session, available):
    """Offer newly search-selected sources a first reading before more discovery.

    Args:
        session: Run with source registration, passage and failure receipts.
        available: Already budget/permission/retry-filtered ready tasks.
    Returns:
        Tasks preparing or probing one unread selected source, plus validation and
        review, or None if none is runnable. Official hints rank the sources. A
        failed/uncertain extraction is an attempted opportunity, not zero gain or
        permission to replay it. Cooling/blocked sources never freeze other work.
        No new quota is granted; the caller applies ordinary bounded review policy.
    """
    if not session.request.extract_knowledge:
        return None
    attempted = {a['source_id'] for a in (session.extraction or {}).get('attempts', [])
        if a['status'] != 'not_attempted'}
    selected = {s.source_id for s in session.sources
        if getattr(s, 'discovered_from_search', None) and s.source_id not in attempted}
    choices = [t for t in available if t['action'] in {'retrieve_sources', 'extract_knowledge'}
        and task_source(session, t) in selected]
    if not choices:
        session.workflow.pop('source_handoff', None)
        return None
    chosen = min(choices, key=lambda t: task_source_priority(session, t))
    chosen['first_read'] = True
    session.workflow['source_handoff'] = {'source_id': task_source(session, chosen),
        'reason': 'read_selected_source_before_more_search'}
    return [chosen] + [t for t in available if t['action'] in {'review_claims', 'validate_evidence'}]


def rotation_work(session, available):
    """Temporarily switch away from a document after repeated completed reads add no useful evidence.

    Try an eligible preferred-domain source, an assessment and search opportunity, or
    another source sample. Recheck gains after waiting reviews: a newly supported claim
    can make the earlier no-gain decision obsolete. Unread passages remain queued.

    Args:
        session: Run with optional no-gain threshold and persistent focus journal.
        available: Ready tasks after all resource/recovery gates.
    Returns:
        A restricted work list, or None to resume ordinary focus. Three successful
        no-gain passages is the new-run default; zero or None disables the policy. First
        try a runnable official alternative, otherwise assess once and search once,
        otherwise probe a peer. Pending reviews flush before the switch. New search
        selections are processed by selected_source_work before this policy resumes.
        Pauses, watermarks and attempted alternatives survive restart. Unread work
        is never removed; unavailable search allows immediate independent progress.
    """
    limit = getattr(session.request, 'max_no_gain_passages', None)
    if not limit:
        return None
    workflow = session.workflow
    yields = document_yields(session)
    workflow['document_yield'] = yields
    focus = workflow.get('document_focus', {})
    rotation = workflow.get('source_rotation', {})
    attempts = (session.extraction or {}).get('attempts', [])
    attempted = {a['batch_id'] for a in attempts if a['status'] != 'not_attempted'}
    if rotation.get('status') != 'active':
        sid = focus.get('source_id')
        outcome = yields.get(sid, {})
        if (focus.get('status') not in {'reading', 'reviewing', 'blocked'}
                or outcome.get('no_gain_passages', 0) < limit):
            return None
        rotation = {'status': 'active', 'source_id': sid, 'attempted_before': sorted(attempted),
            'search_queries_before': len(session.search.get('queries', [])),
            'selections_before': len(session.search.get('selections', [])),
            'coverage_before': sum(c['task'] == 'assess_research_gaps' for c in session.processing_calls),
            'no_gain_passages': outcome['no_gain_passages']}
        workflow['source_rotation'] = rotation
        focus.update(status='paused', release_reason='low_goal_gain')
    sid = rotation['source_id']
    peer_attempted = any(a['source_id'] != sid and a['batch_id'] in attempted
        and a['batch_id'] not in rotation['attempted_before'] for a in attempts)
    selected = len(session.search.get('selections', [])) > rotation['selections_before']
    searched = len(session.search.get('queries', [])) > rotation['search_queries_before']
    # A newly completed review can support a claim from the recent passages.
    # Reconsider the switch if those passages now count as useful progress.
    recovered = yields.get(sid, {}).get('no_gain_passages', 0) < limit
    selection = [t for t in available if t['action'] == 'select_sources']
    if not (recovered or peer_attempted or selected or (searched and not selection)):
        reviews = [t for t in available if t['action'] in {'review_claims', 'validate_evidence'}]
        if reviews:
            return reviews
        peers = [t for t in available if t['action'] in {'retrieve_sources', 'extract_knowledge'}
            and task_source(session, t) not in {None, sid}]
        peers.sort(key=lambda t: (task_source_priority(session, t),
            yields.get(task_source(session, t), {}).get('attempted', 0)))
        official = [t for t in peers if task_source_priority(session, t) == 0]
        if official:
            return official[:1]
        remaining = session.request.budget.max_requests - session.usage.requests_attempted
        coverage = [t for t in available if t['action'] == 'assess_research_gaps']
        from projectclarity.nodes.schedule_work.workflow_control import processing_reserve
        if (coverage and not searched and remaining >= 4 + 2 * session.request.model_retries + processing_reserve(session)
                and sum(c['task'] == 'assess_research_gaps' for c in session.processing_calls)
                == rotation['coverage_before']):
            return coverage[:1]
        search = selection or [t for t in available if t['action'] == 'web_search']
        if search:
            return search[:1]
        if peers:
            return peers[:1]
    rotation.update(status='completed', outcome='progress_recovered' if recovered else 'alternative_opportunity_finished')
    workflow.setdefault('source_rotation_history', []).append(dict(rotation))
    if sid in yields:
        workflow.setdefault('yield_resets', {})[sid] = {
            k: yields[sid][k] for k in ('text_sha256', 'attempted')}
    focus.update(status='released', release_reason='low_goal_gain_rotation_completed')
    return None
