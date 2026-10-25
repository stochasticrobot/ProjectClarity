"""Own durable work scheduling, bounded recovery and operational termination.

LangGraph calls schedule_work between small work units. Existing adapters still
own model/evidence/network operations; no prompt or provider is defined here.
Tasks are deterministic projections of retained inventories, not of the last
node's success flag. session.json is the atomic restart journal. A running task
is reserved before its adapter runs; restart never blindly replays a model call.
Lane rotation applies after discovery.exploration_cycle filters ready tasks.
With that phase gate enabled, breadth inside the relative horizon can consume
the model budget before coverage/search; there is no per-wave call quota.
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

RETRYABLE_FETCH = {'http_timeout', 'connection_failed', 'http_502', 'http_503', 'http_504'}
FETCH_LIMITS = {'run_time_limit', 'request_limit', 'total_size_limit'}
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
    return any(c.get('error_code') == 'provider_or_adapter_error' for c in
        [*session.processing_calls, *(session.extraction or {}).get('attempts', [])])


def fetch_block(session):
    """Resolve the shared acquisition ceiling independently of model-call capacity.

    Args:
        session: Run with refreshed acquisition counters and configured limits.

    Returns:
        A limit code or None. A bad page alone does not block other pages.
    """
    usage, limits = session.retrieval_usage, session.request.fetch_limits
    if usage.get('elapsed_seconds', 0) >= limits.max_run_seconds:
        return 'run_time_limit'
    if usage.get('bytes_read', 0) >= limits.max_total_bytes:
        return 'total_size_limit'
    if usage.get('http_requests', 0) >= limits.max_http_requests:
        return 'request_limit'
    return None


def processing_reserve(session):
    """Reserve a minimum processing opportunity for already registered sources.

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
    failed = {d.source_id for d in session.documents if d.status != 'retrieved'}
    unread = [s for s in session.sources if s.source_id not in attempted | failed]
    return len(unread) * (1 + int(session.request.review_claims))


def work_inventory(session):
    """Project remaining work from authoritative source, passage and proposal ledgers.

    Args:
        session: Mutable run; this function only reads it. Inventories include
            blocked work so limits never make unfinished work disappear.

    Returns:
        Ordered task specifications with lane, action, identity, blocking reason
        and maximum attempts. Payload bodies stay in their original ledgers.
    """
    request = session.request
    tasks = []
    model_reason = ('model_request_limit' if session.usage.requests_attempted >= request.budget.max_requests
        else 'model_provider_unavailable' if model_blocked(session) else None)
    fetch_reason = fetch_block(session)

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
                blocked=None if source.kind == 'file' and fetch_reason == 'request_limit' else fetch_reason,
                attempts=3, source_id=source.source_id)

    extraction = session.extraction or {}
    passages = extraction.get('attempts', [])
    inventoried = {p['source_id'] for p in passages} | {e['source_id'] for e in extraction.get('source_errors', [])}
    new_documents = [d.source_id for d in session.documents if d.status == 'retrieved' and d.source_id not in inventoried]
    if request.extract_knowledge:
        if new_documents:
            add('process', 'extract_knowledge', ['inventory', new_documents], blocked=model_reason)
        served = {sid: sum(p['source_id'] == sid and p['status'] != 'not_attempted' for p in passages)
            for sid in inventoried}
        for passage in sorted(passages, key=lambda p: served.get(p['source_id'], 0)):
            if passage['status'] == 'not_attempted':
                add('process', 'extract_knowledge', passage['batch_id'], blocked=model_reason,
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
            add('process', 'review_claims', unreviewed, blocked=model_reason)
        unadjudicated = [c['candidate_id'] for c in extraction.get('candidates', [])
            if c['candidate_id'] in session.claim_reviews and c.get('adjudication_status') == 'pending']
        if unadjudicated:
            add('process', 'validate_evidence', ['adjudicate', unadjudicated])

    evidence_revision = [(p['batch_id'], p['status']) for p in passages if p['status'] != 'not_attempted']
    if request.assess_coverage and extraction and evidence_revision:
        add('coverage', 'assess_research_gaps', [evidence_revision, len(session.claim_reviews)], blocked=model_reason,
            attempts=2)

    if request.web_search:
        result_ids = pending_search_results(session)
        capacity = request.max_sources - len(session.sources)
        if request.max_search_sources is not None:
            capacity = min(capacity, request.max_search_sources - len(session.search.get('selected_sources', [])))
        if result_ids:
            add('search', 'select_sources', result_ids,
                blocked=model_reason or fetch_reason or ('source_limit' if capacity <= 0 else None), attempts=2)
        queries = proposed_queries(session)
        if queries:
            ready_at, _ = search_readiness(session)
            add('search', 'web_search', [(q['query'], q.get('rounds', 0)) for q in queries[:2]],
                blocked=fetch_reason or search_stop_reason(session), ready_at=ready_at)

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
        blocked = model_reason or fetch_reason or ('source_limit' if len(session.sources) >= request.max_sources else
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
            reason = model_reason or ('knowledge_request_limit' if capture_remaining(session) <= 0 else capture_block)
            quantum = request.knowledge_calls_per_round or 1
            allowed = request.budget.max_requests - session.usage.requests_attempted - (reserve if primary else 0)
            add('capture', 'accumulate_knowledge', [uninventoried,
                [(w.get('window_id', w['source_id']), w['status'], len(w.get('calls', []))) for w in windows]],
                blocked=None if uninventoried else reason, inventory_only=bool(uninventoried and reason),
                max_calls=0 if reason else min(quantum, max(0, allowed)))
    return tasks


def settle_work(session):
    """Close a returned adapter task, preserving failure and retry history.

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
    """Reconcile task projections and classify runnable or delayed journal entries.

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


def schedule_work(state, output_root, monitor=None):
    """Reserve a phase-gated work unit, or persist an explicit operational stop.

    Args:
        state: Typed graph state carrying the durable session journal.
        output_root: Local run-root directory for atomic session snapshots.
        monitor: Optional cancellation/progress interface; no source text is emitted.

    Returns:
        Updated session whose workflow.next names the adapter node, wait, or stop.
        Budgets and task attempts are never reset. Incomplete work is recorded as
        blocked/deferred/failed rather than mistaken for sufficient evidence. Persist
        branch and exploration-cycle decisions without additional model tasks. New
        exploration-enabled runs prioritize runnable analysis and selected link
        expansion before gap assessment/search; legacy runs retain round-robin.
    """
    session = state['session']
    session.visited_nodes.append('schedule_work')
    session.workflow.setdefault('version', 1)
    workflow = session.workflow
    workflow.setdefault('tasks', {})
    prior_cycle = workflow.get('exploration_cycle', {})
    prior_phase = (prior_cycle.get('number'), prior_cycle.get('phase'))
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
    from projectclarity.discovery.exploration_cycle import prepare_cycle, choose_cycle_work
    prepare_cycle(session)
    available, delayed = ready_work(session)
    chosen = choose_cycle_work(session, available, delayed)
    if chosen is None:
        # An unavailable/cooling search must not become a permanent crawl cap.
        # Advancing the roots changes link eligibility, so rebuild the projection.
        available, delayed = ready_work(session)
        chosen = choose_cycle_work(session, available, delayed)
    available = chosen if chosen is not None else available
    if session.status == 'cancelled':
        workflow.update(next='stop', stop_reason='cancelled')
    elif available:
        # Round-robin categories and source-balanced passages prevent page length
        # from deciding all subsequent model allocation. This is not a new quota.
        cursor = workflow.get('cursor', 0)
        lanes = LANES[cursor:] + LANES[:cursor]
        task = min(available, key=lambda t: lanes.index(t['lane']))
        workflow['cursor'] = (LANES.index(task['lane']) + 1) % len(LANES)
        if task['action'] == 'web_search' and workflow.get('exploration_cycle'):
            # Save the next cycle before I/O so an interrupted search cannot
            # replay a prior exploration wave or silently reset its ancestry.
            prepare_cycle(session, 'search_batch_reserved')
        task.update(status='running', attempts=task['attempts'] + 1,
            before_calls=len(session.processing_calls), before_queries=len(session.search.get('queries', [])),
            before_search_round=session.search.get('round', 0),
            before_model_requests=session.usage.requests_attempted, started_at=time.time(),
            before_extraction_batches=[p['batch_id'] for p in (session.extraction or {}).get('attempts', [])
                if p['status'] != 'not_attempted'])
        workflow.update(active=task['task_id'], next=task['action'], stop_reason=None)
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
    if monitor and cycle and (cycle['number'], cycle['phase']) != prior_phase:
        monitor.observe('exploration_phase_changed', exploration_cycle=cycle['number'],
            exploration_phase=cycle['phase'], exploration_depth=cycle['limit'])
    save_session(session, output_root)
    return {'session': session}
