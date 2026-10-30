"""Prioritize existing tasks that can resolve missing or unfinished answer evidence.

The research scheduler supplies tasks already allowed by permissions, budgets and
retry rules. These helpers prefer quotation validation, reading newly selected
sources, claim review, a suggested search, or a completion assessment as appropriate.
For designed reports, missing answer requirements can trigger this priority even
when no total number of results is known. Older plans can trigger it when a known
result target has been reached and still needs review.

Priority markers are recorded in the supplied session so repeated scheduling does
not continually restart the same assessment. This module neither creates research
findings nor runs the selected tasks; choosing a repair does not prove it succeeds.
"""
from projectclarity.research_results import result_progress


def contract_blocker_work(session, available):
    """Choose eligible tasks that can clarify unresolved designed-answer requirements.

    Prefer needed quotation validation, progress on already selected sources, waiting
    claim reviews and evidence-gap searches or assessments. The choices do not require
    a known population total, so explanatory questions can also receive attention.

    Args:
        session: Mutable designed-report run with coverage enabled.
        available: Tasks already admitted by retry, permission and budget gates.
    Returns:
        Preferred validation, selected-source preparation, review, gap-search or
        assessment tasks, or None. Newly search-selected sources get a first
        reading before another search, so queued blockers cannot starve their results.
        At most two review calls precede an eligible queued gap search. The saved
        watermark survives restart; no tasks, quotas or evidence are manufactured.
        Each evidence revision gets at most one initial priority assessment request.
        An active incremental coverage cycle finishes its bounded units before
        expanding discovery, provided inputs have not changed and work is eligible.
    """
    candidates = (session.extraction or {}).get('candidates', [])
    if not candidates:
        return None
    validation = [t for t in available if t['action'] == 'validate_evidence']
    if validation:
        return validation
    from projectclarity.coverage.stopping import evidence_revision
    progress = session.coverage.get('batch_progress', {})
    assessments = [t for t in available if t['action'] == 'assess_research_gaps']
    if (assessments and progress.get('mode') == 'incremental'
            and progress.get('evidence_revision') == evidence_revision(session)
            and session.coverage.get('assessment_status') == 'reviewing'):
        return assessments
    from projectclarity.nodes.schedule_work.source_rotation import selected_source_work
    handoff = selected_source_work(session, available)
    if handoff:
        return handoff[:1]
    marker = session.workflow.setdefault('contract_blocker_focus', {})
    searches = [t for t in available if t['action'] in {'select_sources', 'web_search'}]
    queued = session.coverage.get('next_searches', [])
    attempted = {' '.join(q['query'].split()).casefold() for q in session.search.get('queries', [])}
    queued = [q for q in queued if ' '.join(q['query'].split()).casefold() not in attempted]
    reviews = [t for t in available if t['action'] == 'review_claims']
    if queued and searches:
        queue_key = [q['query'] for q in queued]
        if marker.get('queue') != queue_key:
            marker.update(queue=queue_key, review_start=session.usage.requests_attempted)
        if reviews and session.usage.requests_attempted - marker['review_start'] < 2:
            return reviews
        selections = [t for t in searches if t['action'] == 'select_sources']
        return selections or searches
    if reviews:
        return reviews
    revision = evidence_revision(session)
    assessments = [t for t in available if t['action'] == 'assess_research_gaps']
    history = session.coverage.get('history', [])
    last = history[-1] if history else {}
    if (assessments and last.get('evidence_revision') != revision
            and marker.get('requested_revision') != revision):
        marker['requested_revision'] = revision
        return assessments
    return None


def completion_work(session, available):
    """Prioritize review or assessment when current findings need a completion decision.

    Designed answers use their unresolved requirements; older plans use a reached
    result target. Return a subset of already eligible tasks rather than creating
    new work or overriding a budget restriction.

    Args:
        session: Mutable run with a plan and journal; no model requests occur here.
        available: Already budget/retry-filtered scheduler tasks.
    Returns:
        A preferred task subset or None. An assessment is requested once per
        evidence revision, then recorded gap-search proposals can take precedence.
        Designed reports also prioritize blockers without a numerical target,
        using bounded review flushing before eligible searches. No task is created,
        failed review revived or numerical estimate promoted.
    """
    if not session.plan or not session.request.assess_coverage:
        return None
    if session.answer_schema:
        return contract_blocker_work(session, available)
    goals = result_progress(session.model_dump(mode='json'))['goals']
    reached = [g for g in goals if g['measurement']['target'] is not None
        and g['measurement']['identified'] >= g['measurement']['target']]
    if not reached:
        return None
    reviews = [t for t in available if t['action'] == 'review_claims']
    if reviews:
        return reviews
    from projectclarity.coverage.stopping import evidence_revision
    revision = evidence_revision(session)
    history = session.coverage.get('history', [])
    last = history[-1] if history else {}
    assessments = [t for t in available if t['action'] == 'assess_research_gaps']
    if assessments and last.get('evidence_revision') != revision:
        marker = session.workflow.setdefault('completion_focus', {})
        if marker.get('requested_revision') != revision:
            marker.update(requested_revision=revision, goal_ids=[g['goal_id'] for g in reached])
            return assessments
    if session.coverage.get('next_searches'):
        gap_work = [t for t in available if t['action'] in {'web_search', 'select_sources'}]
        if gap_work:
            return gap_work
    return None
