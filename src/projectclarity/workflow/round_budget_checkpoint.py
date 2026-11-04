"""Ask before spending a heavily used budget on another research round.

Link ranking itself costs model calls, so the decision belongs before ranking,
not after it. This checkpoint is saved in session state and ends the current graph
invocation; reopening a page or restarting the server never grants approval.
It preserves remaining capacity for a report but cannot guarantee that it fits.
"""
from uuid import uuid4


def before_link_ranking(state, caller):
    """Choose ranking, reporting or a durable human pause from actual usage.

    Args:
        state: Mutable session snapshot; the graph caller persists any changes.
        caller: Shared model transport with optional session budget accounting.
    Returns:
        'rank', 'report' or 'wait'. More than half of either current total allowance
        triggers the checkpoint; exactly half does not. Known and estimated token
        use count together. Unknown usage also needs approval, never assumed free.
        An explicit decision applies only to this round. Final rounds skip ranking
        when over the threshold. No model requests or allowance changes occur.
    """
    current = state.get('current_round', 1)
    decision = state.get('round_budget_decision', {})
    if decision.get('round') == current:
        if decision.get('status') == 'pending':
            return 'wait'
        if decision.get('action') == 'continue':
            return 'rank'
        if decision.get('action') == 'report':
            return 'report'
    budgets = getattr(caller, 'budgets', None)
    budget = budgets.snapshot(state['session_id']) if budgets else None
    if not budget:
        return 'rank'
    tokens = budget['known_tokens'] + budget['estimated_tokens']
    if (budget['calls_used'] * 2 <= budget['call_limit']
            and tokens * 2 <= budget['token_limit'] and not budget['unknown_usage']):
        return 'rank'
    usage = {key: budget[key] for key in ('calls_used', 'call_limit', 'known_tokens',
                                        'estimated_tokens', 'token_limit', 'unknown_usage')}
    if current >= state.get('research_rounds', 1):
        state['rounds_finished_reason'] = 'Final round reached; link ranking skipped to preserve the remaining budget.'
        return 'report'
    state['round_budget_decision'] = dict(id=str(uuid4()), round=current,
        status='pending', usage=usage)
    state['status'] = 'link_ranking_waiting'
    state['link_ranking'] = {'status': 'waiting', 'error': None}
    return 'wait'
