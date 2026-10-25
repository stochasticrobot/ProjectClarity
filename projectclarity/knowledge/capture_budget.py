"""Auxiliary capture accounting under the unchanged whole-run safety ceiling."""


def capture_requests(session):
    """Count reserved capture attempts, including failures, from durable call receipts.

    Args:
        session: Research session containing all structured-call receipts.

    Returns:
        Number of started/completed/failed capture calls. Not-attempted reservations
        are excluded; failed calls still consume the allowance.
    """
    return sum(c.get('task') == 'accumulate_knowledge' and c.get('status') in
        {'started', 'completed', 'failed'} for c in session.processing_calls)


def capture_remaining(session):
    """Bound optional broad capture independently without expanding overall spending.

    Args:
        session: Research session with immutable global and auxiliary allowances.

    Returns:
        Remaining authorized capture attempts. Historical None keeps the old shared
        ceiling; explicit zero disables capture. No calls or state changes occur.
    """
    overall = max(0, session.request.budget.max_requests - session.usage.requests_attempted)
    cap = session.request.knowledge_max_requests
    return overall if cap is None else min(overall, max(0, cap - capture_requests(session)))
