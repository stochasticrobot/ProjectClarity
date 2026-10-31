"""Maintain the saved task queue and choose when research continues, waits or stops.

LangGraph calls schedule_work between processing steps. This module reconstructs
remaining tasks from registered sources, passage attempts, review records and search
choices, then applies permissions, request/time limits and retry conditions. It
records a reservation in session.json before returning a task for execution, so a
restart can identify interrupted work without blindly repeating a paid model call.

Task eligibility is separate from task priority. New designed-answer runs use the
saved answer-cycle controller; older or non-designed runs use the older selection
rules. Both use the same budget and completion checks. This file coordinates work
and saves its history; the individual nodes perform network requests, model calls
and evidence processing. Stopping for an exhausted allowance is not an answered
research question.
"""
import hashlib
import json
import time

from projectclarity.discovery.observed_link_inventory import inventory_links
from projectclarity.discovery.selection_acceptance import pending_offered_ids
from projectclarity.nodes.web_search.node import pending_search_results, proposed_queries, search_stop_reason
from projectclarity.discovery.search_engines import search_readiness
from projectclarity.knowledge.capture_budget import capture_remaining
from projectclarity.session_storage import save_session
from projectclarity.providers.model_retry import remaining_model_time
from projectclarity.nodes.schedule_work.document_continuation import document_work
from projectclarity.extraction.document_structure import passage_order
from projectclarity.nodes.schedule_work.source_priority import task_source_priority
from projectclarity.coverage.checkpoints import coverage_checkpoint, coverage_enabled

RETRYABLE_FETCH = {'http_timeout', 'connection_failed', 'http_502', 'http_503', 'http_504'}
FETCH_LIMITS = {'run_time_limit', 'request_limit', 'total_size_limit'}
from projectclarity.coverage.stopping import completion_ready

LANES = ('search', 'retrieve', 'capture', 'process', 'coverage', 'discover')


def identity(value):
    """Fingerprint task inputs without persisting source bodies in task identifiers.

    Args:
        value: JSON-compatible identities or revision counters, not credentials.

    Returns:
        A deterministic hexadecimal digest used to recognize repeated work.
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def model_blocked(session):
    """Recognize a provider-wide failure without treating bad JSON as an outage.

    Args:
        session: Run with cumulative model and extraction receipts.

    Returns:
        True for a saved transport/adapter outage; independent acquisition remains
        possible. This conservative breaker is not reset by ordinary restart.
    """
    return any(c.get('error_code') in {'provider_or_adapter_error', 'model_retry_deferred',
        'model_call_already_recorded'} for c in
        [*session.processing_calls, *(session.extraction or {}).get('attempts', [])])


def fetch_block(session):
    """Resolve the shared acquisition ceiling independently of model-call capacity.

    Args:
        session: Run with acquisition counters, wall-time checkpoint and limits.

    Returns:
        A limit code or None. Include time since the saved checkpoint; a bad page
        alone does not block other pages. No counters or allowances are reset.
    """
    usage, limits = session.retrieval_usage, session.request.fetch_limits
    remaining = remaining_model_time(session)
    if remaining is not None and remaining <= 0:
        return 'run_time_limit'
    if usage.get('bytes_read', 0) >= limits.max_total_bytes:
        return 'total_size_limit'
    if usage.get('http_requests', 0) >= limits.max_http_requests:
        return 'request_limit'
    return None


def processing_reserve(session):
    """Calculate requests to leave available for first processing of registered sources.

    Allow for an extraction and optional review of each viable source still unread.
    This is a minimum planning allowance inside the existing budget, not a promise
    that one request can finish a long document.

    Args:
        session: Run with registered sources, acquired documents and passage receipts.

    Returns:
        Minimum calls for one extraction and optional review on each still-unread
        viable source. This is allocation within the existing allowance, not a new
        quota or a claim that every long document can finish in two calls.
    """
    if not session.request.extract_knowledge:
        return 0
    attempted = {p['source_id'] for p in (session.extraction or {}).get('attempts', [])
        if p['status'] != 'not_attempted'}
    from projectclarity.extraction.content_identity import content_aliases
    attempted.update(content_aliases(session))
    failed = {d.source_id for d in session.documents if d.status != 'retrieved'}
    unread = [s for s in session.sources if s.source_id not in attempted | failed]
    return len(unread) * (1 + int(session.request.review_claims))


def work_inventory(session):
    """List the research tasks implied by saved sources, passages, reviews and search choices.

    Include tasks currently blocked by permissions or limits, so unfinished work does
    not disappear from the record. The list describes work and its restrictions; it
    does not reserve resources or execute any task.

    Args:
        session: Mutable run; this function only reads it. Inventories include
            blocked work so limits never make unfinished work disappear.

    Returns:
        Ordered task specifications with lane, action, identity, blocking reason
        and maximum attempts. Expired model work is blocked before dispatch, not
        converted into individual failed calls. Payload bodies stay in their ledgers.
        Coverage-enabled runs reserve the final model slot; new acquisition also
        waits at that boundary so it cannot invalidate the terminal assessment.
        All coverage identities include evidence revisions and durable
        batch advancement, permitting bounded continuation without resetting retries.
    """
    request = session.request
    tasks = []
    remaining = remaining_model_time(session)
    model_reason = ('run_time_limit' if remaining is not None and remaining <= 0 else
        'model_request_limit' if session.usage.requests_attempted >= request.budget.max_requests
        else 'model_provider_unavailable' if model_blocked(session) else None)
    fetch_reason = fetch_block(session)
    coverage_only = (coverage_enabled(session)
                     and request.budget.max_requests - session.usage.requests_attempted <= 1)
    work_model_reason = model_reason or ('model_request_limit' if coverage_only else None)

    def add(lane, action, key, *, blocked=None, attempts=1, **details):
        """Add one work specification without reserving resources or executing it.

        Args:
            lane: Rotation category, subject to any active exploration-phase gate.
            action: Existing graph node to invoke.
            key: Stable input identity within that action.
            blocked: Current resource or permission restriction, if any.
            attempts: Maximum lifetime executions of these same inputs.
            **details: Small source/batch identifiers needed by the adapter.

        Returns:
            None. Append a specification to this invocation's task inventory.
        """
        tasks.append(dict(task_id=action + ':' + identity(key), lane=lane,
            action=action, blocked=blocked, max_attempts=attempts, **details))

    documents = {d.source_id: d for d in session.documents}
    for source in session.sources:
        doc = documents.get(source.source_id)
        if doc is None or doc.error_code in RETRYABLE_FETCH:
            add('retrieve', 'retrieve_sources', source.source_id,
                blocked=('model_request_limit' if coverage_only else
                         None if source.kind == 'file' and fetch_reason == 'request_limit' else fetch_reason),
                attempts=3, source_id=source.source_id)

    extraction = session.extraction or {}
    passages = extraction.get('attempts', [])
    inventoried = {p['source_id'] for p in passages} | {e['source_id'] for e in extraction.get('source_errors', [])}
    from projectclarity.extraction.content_identity import content_aliases
    inventoried.update(content_aliases(session))
    new_documents = [d.source_id for d in session.documents if d.status == 'retrieved' and d.source_id not in inventoried]
    if request.extract_knowledge:
        for sid in new_documents:
            add('process', 'extract_knowledge', ['inventory', sid], blocked=work_model_reason, source_id=sid)
        for passage in passage_order(session, passages):
            if passage['status'] == 'not_attempted':
                add('process', 'extract_knowledge', passage['batch_id'], blocked=work_model_reason,
                    batch_id=passage['batch_id'])
        # Restart may occur between extraction, evidence checking and review.
        completed = [p['batch_id'] for p in passages if p['status'] == 'completed']
        finalized = set(session.workflow.get('finalized_batches', []))
        if any(b not in finalized for b in completed):
            add('process', 'validate_evidence', ['finalize', completed], batch_ids=completed)

    if request.review_claims:
        unreviewed = [c['candidate_id'] for c in extraction.get('candidates', [])
            if c['evidence_status'] == 'anchored' and c['candidate_id'] not in session.claim_reviews]
        if unreviewed:
            add('process', 'review_claims', unreviewed, blocked=work_model_reason)
        unadjudicated = [c['candidate_id'] for c in extraction.get('candidates', [])
            if c['candidate_id'] in session.claim_reviews and c.get('adjudication_status') == 'pending']
        if unadjudicated:
            add('process', 'validate_evidence', ['adjudicate', unadjudicated])

    evidence_revision = [(p['batch_id'], p['status']) for p in passages if p['status'] != 'not_attempted']
    if request.assess_coverage and extraction and evidence_revision:
        from projectclarity.coverage.stopping import evidence_revision as current_revision
        from projectclarity.coverage.contract_assessment import batch_schedule_key
        coverage_key = [current_revision(session), batch_schedule_key(session)]
        add('coverage', 'assess_research_gaps', coverage_key, blocked=model_reason,
            attempts=2)

    if request.web_search:
        remaining_calls = request.budget.max_requests - session.usage.requests_attempted
        # Selecting a lead without room to read it is not a useful search round.
        reserve = processing_reserve(session)
        minimum = (1 + request.model_retries + int(coverage_enabled(session))
                   + (1 + int(request.review_claims) if request.extract_knowledge else 0))
        search_budget = 'model_request_limit' if remaining_calls < reserve + minimum else None
        result_ids = pending_search_results(session)
        capacity = request.max_sources - len(session.sources)
        if request.max_search_sources is not None:
            capacity = min(capacity, request.max_search_sources - len(session.search.get('selected_sources', [])))
        if result_ids:
            add('search', 'select_sources', result_ids,
                blocked=work_model_reason or fetch_reason or search_budget or ('source_limit' if capacity <= 0 else None), attempts=2)
        queries = proposed_queries(session)
        if queries:
            ready_at, _ = search_readiness(session)
            add('search', 'web_search', [(q['query'], q.get('rounds', 0)) for q in queries[:2]],
                blocked=work_model_reason or fetch_reason or search_budget or search_stop_reason(session), ready_at=ready_at)

    if request.follow_links and session.documents:
        links = inventory_links(session)
        receipts = [*session.discovery.get('history', [])]
        if session.discovery.get('selection_attempted'):
            receipts.append(session.discovery)
        eligible = [l for l in links if l['eligible']]
        pending = pending_offered_ids(eligible, receipts, 'offered_link_ids', 'capacity_deferred_ids', 'stop_reason')
        # One empty-frontier closure updates discovery receipts without a model call.
        key = pending if pending else ['closure', [(l['link_id'], l['eligible'], l['exclusion_reason']) for l in links]]
        remaining = request.budget.max_requests - session.usage.requests_attempted
        blocked = work_model_reason or fetch_reason or ('source_limit' if len(session.sources) >= request.max_sources else
            'model_request_limit' if remaining < 3 + processing_reserve(session) else None)
        add('discover', 'select_followup_sources', key, blocked=blocked if pending else None, attempts=2)

    if request.accumulate_knowledge:
        windows = session.knowledge.get('windows', [])
        captured_sources = {w['source_id'] for w in windows}
        uninventoried = [d.source_id for d in session.documents if d.status == 'retrieved' and d.source_id not in captured_sources]
        if uninventoried or any(w['status'] == 'pending' for w in windows):
            primary = any(t['lane'] != 'capture' and not t['blocked']
                and session.workflow.get('tasks', {}).get(t['task_id'], {}).get('status') not in {'completed', 'failed', 'uncertain'} for t in tasks)
            reserve = 1 + int(request.review_claims) + int(request.assess_coverage) + int(request.follow_links or request.web_search)
            capture_block = ('calls_reserved_for_research' if primary
                and request.budget.max_requests - session.usage.requests_attempted <= reserve else None)
            reason = work_model_reason or ('knowledge_request_limit' if capture_remaining(session) <= 0 else capture_block)
            quantum = request.knowledge_calls_per_round or 1
            allowed = request.budget.max_requests - session.usage.requests_attempted - (reserve if primary else 0)
            add('capture', 'accumulate_knowledge', [uninventoried,
                [(w.get('window_id', w['source_id']), w['status'], len(w.get('calls', []))) for w in windows]],
                blocked=None if uninventoried else reason, inventory_only=bool(uninventoried and reason),
                max_calls=0 if reason else min(quantum, max(0, allowed)))
    return tasks


def settle_work(session):
    """Record the outcome of the previously reserved task after its processing steps return.

    Update its attempt and retry history and clear the active reservation. A failed
    source or model operation does not by itself mean all independent research must stop.

    Args:
        session: Run after an adapter and its required evidence/review chain.

    Returns:
        None. Update the active journal record, set bounded retry readiness, and
        clear the active reservation. Does not interpret a failed action as a run stop.
    """
    workflow = session.workflow
    active = workflow.get('active')
    if not active:
        return
    task = workflow['tasks'][active]
    action, error = task['action'], None
    if action == 'retrieve_sources':
        doc = next((d for d in session.documents if d.source_id == task['source_id']), None)
        error = doc.error_code if doc else 'retrieval_adapter_failed'
    elif action == 'select_followup_sources':
        reason = session.discovery.get('stop_reason')
        if reason in {'invalid_selection_references', 'invalid_output_schema', 'incomplete_model_output',
                      'selection_failed', 'frontier_context_blocked', 'input_too_large', 'provider_unavailable'}:
            error = reason
    elif action == 'select_sources':
        error = (session.search.get('selections') or [{}])[-1].get('error_code')
    elif action == 'assess_research_gaps':
        error = (session.coverage.get('history') or [{}])[-1].get('error_code')
    elif action == 'review_claims':
        error = next((c.get('error_code') for c in session.processing_calls[task['before_calls']:] if c.get('error_code')), None)
    elif action == 'web_search':
        error = next((q.get('error_code') for q in session.search.get('queries', [])
            if q.get('execution_round', q.get('round', 0)) > task.get('before_search_round', 0)
            and q.get('error_code') and q['status'] == 'failed'), None)
    elif action in {'extract_knowledge', 'validate_evidence'}:
        attempts = (session.extraction or {}).get('attempts', [])
        workflow['finalized_batches'] = [p['batch_id'] for p in attempts if p['status'] == 'completed']
        prior_attempted = set(task.get('before_extraction_batches', []))
        error = next((p.get('error_code') for p in attempts if p['batch_id'] not in prior_attempted
            and p.get('error_code') and p['status'] == 'failed'), None)
    elif action == 'accumulate_knowledge':
        error = next((c.get('error_code') for c in session.processing_calls[task['before_calls']:] if c.get('error_code')), None)
    retryable = error in RETRYABLE_FETCH or (action in {'select_sources', 'select_followup_sources', 'assess_research_gaps'}
        and error in {'invalid_output_schema', 'incomplete_model_output', 'invalid_selection_references',
                      'invalid_search_selection', 'selection_failed'})
    task.update(status='pending' if retryable and task['attempts'] < task['max_attempts'] else 'failed' if error else 'completed',
        error_code=error)
    if error in RETRYABLE_FETCH:
        # Delay is persisted as an absolute readiness time; other tasks run first.
        task['ready_at'] = time.time() + max(2 ** (task['attempts'] - 1), doc.retry_after_seconds or 0)
    task.setdefault('outcomes', []).append({'attempt': task['attempts'], 'error_code': error,
        'model_requests_after': session.usage.requests_attempted})
    workflow['active'] = None


def ready_work(session):
    """Compare remaining task descriptions with saved execution history to find runnable work.

    Completed, failed and uncertain attempts retain their status. A temporary restriction
    can clear, or a previously superseded task can return if its inputs become relevant
    again, but this does not reset budgets or grant unlimited attempts.

    Args:
        session: Mutable run with authoritative inventories and persisted attempts.

    Returns:
        Ready and delayed task lists. Preserve completed/failed/uncertain tasks,
        refresh transient blocking, and close superseded projections. Reopen a
        superseded projection if its inputs return and attempts remain; projection
        disappearance is not proof that a selector considered deferred siblings. No adapter
        runs and no budgets are reset; callers persist the final reservation.
    """
    workflow = session.workflow
    specs = work_inventory(session)
    current_ids = {spec['task_id'] for spec in specs}
    for task_id, task in workflow['tasks'].items():
        if task_id not in current_ids and task['status'] in {'pending', 'blocked'}:
            task.update(status='completed', resolution='reconciled_from_retained_work')
    available, delayed = [], []
    for spec in specs:
        task = workflow['tasks'].setdefault(spec['task_id'], {**spec, 'status': 'pending', 'attempts': 0})
        if (task['status'] == 'completed' and task.get('resolution') == 'reconciled_from_retained_work'
                and task['attempts'] < task['max_attempts']):
            task['status'] = 'pending'
            task.pop('resolution')
        task.update({k: spec[k] for k in ('blocked', 'inventory_only', 'max_calls') if k in spec})
        if task['action'] == 'web_search':
            task['ready_at'] = spec.get('ready_at', 0)
        if task['blocked'] and task['action'] == 'web_search':
            session.search['stop_reason'] = 'fetch_budget_exhausted' if task['blocked'] in FETCH_LIMITS else task['blocked']
        if task.get('ready_at', 0) - time.time() >= session.request.fetch_limits.max_run_seconds - session.retrieval_usage.get('elapsed_seconds', 0):
            task['blocked'] = task['blocked'] or 'retry_after_exceeds_time_budget'
        if task['status'] in {'completed', 'failed', 'uncertain'}:
            continue
        if task['blocked']:
            task['status'] = 'blocked'
        elif task['attempts'] < task['max_attempts']:
            task['status'] = 'pending'
            (delayed if task.get('ready_at', 0) > time.time() else available).append(task)
    return available, delayed


def choose_legacy_work(session, available, delayed):
    """Choose eligible work using the scheduling rules retained for older or non-designed runs.

    These rules coordinate document focus, periodic coverage checks, link exploration
    and search. Runs explicitly using the answer-cycle controller do not enter this
    path, so loading an older run does not silently replace its scheduling policy.

    Args:
        session: Mutable legacy run whose original focus and exploration state remains valid.
        available: Tasks already admitted by the resource/retry projection.
        delayed: Cooling tasks used by the legacy exploration policy.
    Returns:
        Chosen eligible tasks and refreshed delayed tasks. Mutates legacy policy
        state only; new answer-controller runs never enter this adapter.
    """
    from projectclarity.discovery.exploration_cycle import choose_cycle_work
    from projectclarity.claim_review.review_queue import review_work
    from projectclarity.nodes.schedule_work.source_rotation import selected_source_work, rotation_work
    checkpoint = coverage_checkpoint(session, available)
    if checkpoint is None:
        from projectclarity.coverage.completion_work import completion_work
        checkpoint = completion_work(session, available)
    handoff = selected_source_work(session, available) if checkpoint is None else None
    rotating = rotation_work(session, available) if checkpoint is None and handoff is None else None
    focused = document_work(session, available) if checkpoint is None and handoff is None and rotating is None else None
    if checkpoint is not None:
        available = checkpoint
    elif handoff is not None:
        available = review_work(session, handoff)
    elif rotating is not None:
        available = rotating
    elif focused is not None:
        # Keep review batching within focused reading or its bounded peer probe;
        # neither policy adds to the shared request allowance.
        available = review_work(session, focused)
    else:
        available = review_work(session, available)
        chosen = choose_cycle_work(session, available, delayed)
        if chosen is None:
            # An unavailable/cooling search must not become a permanent crawl cap.
            # Advancing roots changes eligibility, so rebuild the projection once.
            available, delayed = ready_work(session)
            available = review_work(session, available)
            chosen = choose_cycle_work(session, available, delayed)
        available = chosen if chosen is not None else available
    return available, delayed


def schedule_work(state, output_root, monitor=None):
    """Choose and save the next research task, a retry wait, or a reason to stop.

    First settle earlier work and enforce cancellation, resource limits and current
    completion checks. Then choose among eligible tasks using the run's recorded policy
    and save a reservation before another workflow step executes it. A stopped run
    can still contain blocked, deferred or failed work and an incomplete answer.

    Args:
        state: Typed graph state carrying the durable session journal.
        output_root: Local run-root directory for atomic session snapshots.
        monitor: Optional cancellation/progress interface; no source text is emitted.

    Returns:
        Updated session whose workflow.next names the adapter node, wait, or stop.
        Budgets and task attempts are never reset. Resource/retry eligibility is
        projected first, then exactly one versioned controller chooses progression.
        New answer-contract runs use the persisted answer cycle; other runs use an
        isolated legacy adapter. Neither can bypass cancellation, the fresh-evidence
        completion gate or execution reservations. One final assessment opportunity
        remains within the existing model allowance. Unfinished work is recorded as
        blocked/deferred/failed rather than mistaken for sufficient evidence.
        Reading, search and coverage reservations retain compact decision snapshots:
        eligible actions, phase/reason, recent gain allocation, backlog and coverage round.
        These diagnostics do not change eligibility, ordering or request allowances.
    """
    session = state['session']
    session.visited_nodes.append('schedule_work')
    session.workflow.setdefault('version', 1)
    workflow = session.workflow
    workflow.setdefault('tasks', {})
    prior_cycle = workflow.get('exploration_cycle', {})
    prior_phase = (prior_cycle.get('number'), prior_cycle.get('phase'))
    prior_focus = workflow.get('document_focus', {})
    prior_document = (prior_focus.get('source_id'), prior_focus.get('status'))
    settle_work(session)
    from projectclarity.discovery.branch_progress import branch_progress
    prior_branches = workflow.get('branch_progress', {}).get('sources', {})
    workflow['branch_progress'] = branch_progress(session)
    if monitor:
        for sid, entry in workflow['branch_progress'].get('sources', {}).items():
            if not entry['seed'] and entry != prior_branches.get(sid):
                monitor.observe('branch_progress_changed', source_id=sid,
                    branch_status=entry['expansion_block'] or entry['status'],
                    unproductive_count=entry['unproductive_count'],
                    branch_limit=workflow['branch_progress']['limit'])
    if monitor:
        try:
            monitor.check_cancel()
        except (KeyboardInterrupt, EOFError):
            session.status = 'cancelled'
        except Exception as exc:
            from projectclarity.retrieval.retrieval_limits import RunCancelled
            if not isinstance(exc, RunCancelled):
                raise
            session.status = 'cancelled'
    from projectclarity.discovery.exploration_cycle import prepare_cycle
    from projectclarity.nodes.schedule_work.answer_controller import enabled as answer_controlled, choose_work
    controlled = answer_controlled(session)
    if not controlled:
        prepare_cycle(session)
    available, delayed = ready_work(session)
    diagnostic_available = list(available)
    complete = session.status != 'cancelled' and completion_ready(session, output_root)
    if session.status != 'cancelled' and not complete:
        if controlled:
            available = choose_work(session, available)
        else:
            available, delayed = choose_legacy_work(session, available, delayed)
    if session.status == 'cancelled':
        workflow.update(next='stop', stop_reason='cancelled')
    elif complete:
        workflow.update(next='stop', stop_reason='criteria_satisfied',
            stop_details=['model_assessed_sufficiency_not_independent_completeness'])
    elif available:
        # Round-robin categories and source-balanced passages apply outside an
        # active productive-document focus. No policy adds resource allowance.
        cursor = workflow.get('cursor', 0)
        lanes = LANES[cursor:] + LANES[:cursor]
        task = min(available, key=lambda t: (lanes.index(t['lane']), task_source_priority(session, t)))
        if task['action'] == 'extract_knowledge' and task.get('batch_id'):
            # Reserve only passages admitted by this scheduling decision. A peer
            # probe stays a single probe; workers cannot bypass focus/phase gates.
            task['batch_ids'] = [t['batch_id'] for t in [task, *available]
                if t['action'] == 'extract_knowledge' and t.get('batch_id')]
            task['batch_ids'] = list(dict.fromkeys(task['batch_ids']))[:session.request.model_concurrency]
        workflow['cursor'] = (LANES.index(task['lane']) + 1) % len(LANES)
        if not controlled and task['action'] == 'web_search' and workflow.get('exploration_cycle'):
            # Save the next cycle before I/O so an interrupted search cannot
            # replay a prior exploration wave or silently reset its ancestry.
            prepare_cycle(session, 'search_batch_reserved')
        task.update(status='running', attempts=task['attempts'] + 1,
            reserve_coverage_call=coverage_enabled(session) and task['action'] != 'assess_research_gaps',
            before_calls=len(session.processing_calls), before_queries=len(session.search.get('queries', [])),
            before_search_round=session.search.get('round', 0),
            before_model_requests=session.usage.requests_attempted, started_at=time.time(),
            before_extraction_batches=[p['batch_id'] for p in (session.extraction or {}).get('attempts', [])
                if p['status'] != 'not_attempted'])
        workflow.update(active=task['task_id'], next=task['action'], stop_reason=None)
        if task['action'] in {'web_search', 'assess_research_gaps', 'extract_knowledge'}:
            from projectclarity.coverage.assessment_diagnostics import scheduling_snapshot
            task.setdefault('decision_history', []).append(scheduling_snapshot(session, diagnostic_available))
    elif delayed:
        workflow.update(next='wait', wait_until=min(t['ready_at'] for t in delayed), stop_reason=None)
    else:
        blocked = [t for t in workflow['tasks'].values() if t['status'] in {'blocked', 'failed', 'uncertain'}
            and not (t['lane'] == 'capture' and session.request.knowledge_max_requests is not None)]
        reasons = sorted({t.get('blocked') or t.get('error_code') or 'uncertain' for t in blocked})
        reason = 'budget_exhausted' if any(r in {'model_request_limit', 'request_limit', 'run_time_limit',
            'total_size_limit', 'source_limit', 'search_query_limit', 'search_source_limit'} for r in reasons) else 'blocked' if blocked else 'no_runnable_work'
        if reason == 'no_runnable_work' and workflow['branch_progress']['enabled']:
            branch_blocks = {link['exclusion_reason'] for link in inventory_links(session)}
            if 'branch_path_search_limit' in branch_blocks:
                reason, reasons = 'blocked', ['branch_path_search_limit']
            elif 'branch_unproductive_limit' in branch_blocks:
                reason = 'branch_limit_reached'
        workflow.update(next='stop', stop_reason=reason, stop_details=reasons)
        if reason not in {'no_runnable_work', 'branch_limit_reached'} and session.status not in {'cancelled', 'failed'}:
            session.status = 'extraction_partial' if session.extraction else 'retrieval_partial' if any(d.status == 'retrieved' for d in session.documents) else 'retrieval_failed'
        if not session.documents and not session.request.plan_only:
            session.status = 'retrieval_failed'
            if not session.sources and not session.request.web_search:
                session.error = 'No sources supplied. Add --urls or --document, or use --plan-only.'
        if 'model_request_limit' in reasons:
            for passage in (session.extraction or {}).get('attempts', []):
                if passage['status'] == 'not_attempted':
                    passage['error_code'] = 'request_limit'
    if session.request.accumulate_knowledge and capture_remaining(session) <= 0 and session.knowledge:
        session.knowledge['pause_reason'] = 'knowledge_request_limit' if session.usage.requests_attempted < session.request.budget.max_requests else 'request_limit'
    workflow['queue_counts'] = {status: sum(t['status'] == status for t in workflow['tasks'].values())
        for status in ('pending', 'running', 'blocked', 'failed', 'uncertain', 'completed')}
    cycle = workflow.get('exploration_cycle')
    focus = workflow.get('document_focus', {})
    if monitor and focus and (focus.get('source_id'), focus.get('status')) != prior_document:
        monitor.observe('document_focus_changed', source_id=focus['source_id'],
            document_focus_status=focus['status'], remaining_passages=focus.get('remaining_passages', 0),
            stop_reason=focus.get('release_reason'))
    if monitor and cycle and (cycle['number'], cycle['phase']) != prior_phase:
        monitor.observe('exploration_phase_changed', exploration_cycle=cycle['number'],
            exploration_phase=cycle['phase'], exploration_depth=cycle['limit'])
    save_session(session, output_root)
    return {'session': session}
