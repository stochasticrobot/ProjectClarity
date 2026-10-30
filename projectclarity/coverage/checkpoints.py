"""Reserve opportunities to assess coverage within the existing model-call budget.

The scheduler uses coverage_checkpoint to select validation, a bounded review
flush, or a coverage assessment when a checkpoint is due. A checkpoint is a
scheduling opportunity, not a saved research result or proof of completeness.
model_request_ceiling keeps ordinary scheduled work from consuming the final
reserved assessment request. Time limits can still prevent that request, and a
failed assessment does not earn extra allowance. This module mutates checkpoint
bookkeeping only; it neither invokes a model nor persists the session itself."""

COVERAGE_CALL_INTERVAL = 12
MAX_CHECKPOINT_REVIEW_CALLS = 2


def coverage_enabled(session):
    """Identify runs that can assess collected evidence against explicit criteria.

    Args:
        session: Typed research run with its immutable operator policy and plan.
    Returns:
        True when extraction and coverage are enabled and the plan has criteria.
        Automatic stopping and provisional population estimates are irrelevant.
    """
    return bool(session.request.assess_coverage and session.request.extract_knowledge
                and session.plan and session.plan.completion_criteria)


def model_request_ceiling(session, task):
    """Return the cumulative model-call limit for the task about to run.

    Ordinary scheduled work must leave one request for the final coverage assessment.
    Batch summaries also preserve that final whole-answer request. This helper reads
    the reservation and task type; it does not reserve or spend a request itself.

    Args:
        session: Run with a durable active task reservation and global allowance.
        task: Model operation being dispatched, not a model-supplied task name.
    Returns:
        Effective cumulative request ceiling. Scheduled non-coverage work leaves
        one request for assessment; coverage, planning and unscheduled adapters
        retain the global ceiling. Evidence digests always leave the final global
        assessment slot available. No usage counters or operator limits change.
    """
    active = session.workflow.get('tasks', {}).get(session.workflow.get('active'), {})
    reserved = task == 'summarize_evidence' or (
        active.get('reserve_coverage_call', False) and task != 'assess_research_gaps')
    return max(0, session.request.budget.max_requests - int(reserved))


def coverage_checkpoint(session, available):
    """Offer evidence validation, claim review or coverage assessment when a check is due.

    A checkpoint here is a scheduling opportunity to assess collected evidence, not a
    saved LangGraph checkpoint. Periodic checks and a reserved final request keep a
    long reading sequence from consuming every opportunity to assess the answer.

    Args:
        session: Mutable run with cumulative call usage and persisted task history.
        available: Ready, budget-checked tasks before source-focus filtering.
    Returns:
        Validation, bounded review flushing, or coverage tasks when due; otherwise
        None. Checks run independently of stopping after twelve model calls, or
        at the final reserved request. At most two review calls delay a periodic check;
        pending claims stay visible and prevent completion. The last slot goes to
        coverage even with reviews pending. Failed/uncertain tasks are never made
        runnable here, and the flush watermark survives restart. A reserved call
        is an opportunity, not a guarantee of a successful or complete assessment.
    """
    if not coverage_enabled(session):
        return None
    previous = [t.get('before_model_requests', 0) for t in session.workflow.get('tasks', {}).values()
                if t['action'] == 'assess_research_gaps' and t.get('attempts', 0)]
    baseline = max(previous, default=0)
    used = session.usage.requests_attempted
    remaining = session.request.budget.max_requests - used
    if used - baseline < COVERAGE_CALL_INTERVAL and remaining > 1:
        return None
    coverage = [t for t in available if t['action'] == 'assess_research_gaps']
    if not coverage:
        return None
    marker = session.workflow.setdefault('coverage_checkpoint', {})
    if marker.get('baseline') != baseline:
        marker.clear()
        marker.update(baseline=baseline, flush_started_at=used)
    marker['reason'] = 'request_budget' if remaining <= 1 else 'periodic'
    validation = [t for t in available if t['action'] == 'validate_evidence']
    if validation:
        return validation
    reviews = [t for t in available if t['action'] == 'review_claims']
    if reviews and remaining > 1 and used - marker['flush_started_at'] < MAX_CHECKPOINT_REVIEW_CALLS:
        return reviews
    return coverage
