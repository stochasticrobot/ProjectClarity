"""Check each proposed source choice against the IDs actually offered to the model.

Search-result and observed-link selectors share these rules. A model may choose an
existing ID, but cannot create a new URL by returning a plausible identifier.
Helpers separate accepted, invalid, duplicate and capacity-deferred choices so one
bad proposal does not discard valid neighbors. Unseen or deferred choices can stay
available for a later request. These checks do not fetch or semantically review pages.
"""
from projectclarity.discovery.observed_link_inventory import canonical_url
from projectclarity.discovery.url_policy import url_denial


def accept_choices(session, choices, offered, id_field, capacity):
    """Validate source-selection proposals separately so one bad choice does not discard valid neighbors.

    Check offered IDs, goal links, explicit relevance decisions and selection capacity.
    Separate accepted, rejected and deferred choices; none of these checks establishes
    that the selected page contains truthful or sufficient evidence.

    Args:
        session: Run containing authorized goals, URL policy and registered sources.
        choices: Parsed model choices; top-level/schema failures never enter here.
        offered: Exact ID-to-inventory mapping supplied to this model request.
        id_field: Choice attribute containing the offered link or result ID.
        capacity: Maximum new URLs allowed this turn under existing source limits.

    Returns:
        Accepted choice objects and per-item receipt dictionaries. Valid overflow
        is deferred; duplicate IDs/URLs and policy violations are recorded. No
        sources are registered and no model/network call is made here. Versioned choices
        require explicit requirement links; rejected dispositions are declined, not
        fetched, regardless of list membership or accompanying prose.
    """
    from projectclarity.discovery.source_disposition import disposition_error
    goals = {g.goal_id for g in session.plan.goals}
    urls = {canonical_url(s.url) for s in session.sources if s.kind == 'url'}
    seen, accepted, outcomes = set(), [], []
    for choice in choices:
        identifier = getattr(choice, id_field)
        row = offered.get(identifier)
        reason = None
        if identifier in seen:
            reason = 'duplicate_id'
        elif row is None:
            reason = 'unknown_or_unoffered_id'
        elif set(choice.goal_ids) - goals:
            reason = 'unknown_goal'
        elif error := disposition_error(session, choice):
            reason = error
        elif not canonical_url(row['url']):
            reason = 'invalid_url'
        else:
            reason = url_denial(row['url'], session.request.url_policy)
            if not reason and canonical_url(row['url']) in urls:
                reason = 'duplicate_url'
        seen.add(identifier)
        outcome = {**choice.model_dump(mode='json'), 'status': 'rejected', 'reason_code': reason}
        if not reason and getattr(choice, 'disposition', None) == 'reject':
            outcome.update(status='declined', reason_code='model_rejected')
        elif not reason:
            urls.add(canonical_url(row['url']))
            if len(accepted) < capacity:
                accepted.append(choice)
                outcome['status'] = 'accepted'
            else:
                outcome.update(status='deferred', reason_code='capacity')
        outcomes.append(outcome)
    return accepted, outcomes


def pending_offered_ids(rows, receipts, offered_key, deferred_key, failure_key):
    """Retain unseen/deferred inventory and allow one retry after unusable replies.

    Args:
        rows: Eligible inventory dictionaries with a link_id or result_id.
        receipts: Chronological selection attempts, including complete failures.
        offered_key: Receipt key listing exactly the IDs supplied to that call.
        deferred_key: Receipt key listing capacity-deferred choices or unmentioned
            offers retained because the selector filled its available batch.
        failure_key: Receipt key with the application failure classification.

    Returns:
        IDs still available to select. A valid response closes its offered items
        except capacity-deferred inventory; an unusable response consumes one of two tries.
        Invalid individual choices do not reopen an otherwise usable batch.
    """
    failures, closed = {}, set()
    recoverable = {'invalid_output_schema', 'incomplete_model_output',
        'invalid_selection_references', 'invalid_search_selection', 'selection_failed'}
    for receipt in receipts:
        offered = receipt.get(offered_key) or []
        unusable = receipt.get(failure_key) in recoverable and not receipt.get('accepted_ids')
        for identifier in offered:
            if unusable:
                failures[identifier] = failures.get(identifier, 0) + 1
                if failures[identifier] >= 2:
                    closed.add(identifier)
            else:
                closed.add(identifier)
        closed.difference_update(receipt.get(deferred_key) or [])
    return [row.get('link_id', row.get('result_id')) for row in rows
        if row.get('link_id', row.get('result_id')) not in closed]
