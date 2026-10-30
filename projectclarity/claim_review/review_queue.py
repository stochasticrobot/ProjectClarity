"""Choose which extracted claims should be reviewed together next.

After quotation validation, claims wait for a model to assess whether their source
passages actually support them. The scheduler calls these helpers to collect a
size-limited group while preserving each claim's original passage, and to flush
waiting reviews before other work or budget limits delay them further. Recovery
marks interrupted review reservations without resending a request whose outcome
is unknown. These helpers update the in-memory work records; callers perform the
model requests and save the session.
"""

MAX_REVIEW_CLAIMS = 12  # A review request may contain at most as many claims as its response schema accepts.
MAX_REVIEW_PASSAGES = 4


def pending_candidates(session):
    """Find claims with located source quotations that still lack a recorded review outcome.

    Keep their extraction order so review scheduling can group waiting claims without
    changing which passage each claim belongs to.

    Args:
        session: Saved run whose candidates and review receipts are authoritative.

    Returns:
        Candidate dictionaries awaiting review. Failed reviews are not auto-retried.
    """
    return [c for c in (session.extraction or {}).get('candidates', [])
        if c['evidence_status'] == 'anchored' and c['candidate_id'] not in session.claim_reviews]


def review_work(session, available):
    """Decide when waiting claims should receive a model support review.

    Collect claims into size-limited batches, but offer review sooner when dependencies
    or the remaining allowance would otherwise leave them unreviewed.

    Args:
        session: Run with pending candidates and remaining request allowance.
        available: Ready scheduler tasks, already filtered for budgets and attempts.

    Returns:
        Task subset prioritizing a due review, or deferring it while more evidence
        can be collected. No-ready-extraction flushes even a single claim so branch
        progress cannot deadlock waiting for the next page. During document focus,
        ordinary batch/low-budget bounds still apply but the crawl checkpoint does
        not force a review after every subsequent passage. No requests are made.
    """
    reviews = [t for t in available if t['action'] == 'review_claims']
    if not reviews:
        return available
    pending = pending_candidates(session)
    from projectclarity.discovery.exploration_cycle import exploration_budget_due
    collecting = any(t['action'] in {'extract_knowledge', 'retrieve_sources'} for t in available)
    due = (len(pending) >= MAX_REVIEW_CLAIMS
        or len({c['batch_id'] for c in pending}) >= MAX_REVIEW_PASSAGES
        or session.request.budget.max_requests - session.usage.requests_attempted <= 2
        or (exploration_budget_due(session)
            and session.workflow.get('document_focus', {}).get('status') != 'reading')
        or not collecting)
    return reviews if due else [t for t in available if t['action'] != 'review_claims']


def recover_review_batches(session):
    """Mark interrupted review reservations without sending their requests again.

    A saved running batch may have reached the provider before the process stopped.
    Its outcome must remain explicit instead of assuming either success or no charge.

    Args:
        session: Reloaded run with batch-to-candidate assignments saved before calls.

    Returns:
        None. Mark only the reserved batch's unresolved claims as explicit failed
        reviews; untouched queued claims remain eligible. Caller persists the run.
    """
    for call_id, batch in session.workflow.get('review_batches', {}).items():
        if batch['status'] != 'reserved':
            continue
        batch.update(status='uncertain', error_code='interrupted_outcome_unknown')
        for candidate_id, assignment in batch['assignments'].items():
            session.claim_reviews.setdefault(candidate_id, {
                'validation_version': 'source-quotes-v2', 'assessment': None,
                'error_code': 'interrupted_outcome_unknown', 'evidence': [],
                'text_sha256': assignment['text_sha256'], 'call_id': call_id})
