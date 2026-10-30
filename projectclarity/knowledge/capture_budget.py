"""Limit optional broad knowledge capture within the overall model-call allowance.

A run can separately opt into capturing entities and relationships beyond its
answer-focused extraction. These helpers count reserved capture attempts, including
failed attempts, and calculate what remains under both the capture limit and the
whole-run limit. They only inspect session records; they do not reserve a request,
call a model or grant additional allowance.
"""


def capture_requests(session):
    """Count reserved capture attempts, including failures, from durable call receipts.

    Args:
        session: Research session containing all structured-call receipts.

    Returns:
        Number of reserved wire attempts, including retries, plus legacy calls lacking
        a wire ledger. Not-attempted logical reservations are excluded; failed and
        uncertain wire attempts still consume the allowance without double-counting.
    """
    legacy = sum(c.get('task') == 'accumulate_knowledge' and c.get('status') in
        {'started', 'completed', 'failed'} and c.get('attempt_accounting') != 'wire_ledger'
        for c in session.processing_calls)
    return legacy + sum(a['task'] == 'accumulate_knowledge' for a in session.model_attempts)


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
