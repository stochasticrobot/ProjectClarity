"""Choose the next kind of research work for runs with explicit answer requirements.

The scheduler first removes tasks forbidden by permissions, limits or retry rules.
For runs marked with this controller's policy, choose_work then cycles through
reading a document, sampling other sources, reviewing claims, assessing answer
requirements and finding more sources. Its phase and counters are saved in the
session so restarting or receiving a new model suggestion does not reset the turn.

For example, discovering another possible search should not continually interrupt
reading a document already acquired. The controller returns task choices and
updates bookkeeping; the scheduler reserves and executes them. Older runs retain
their older selection policy. Attempt counts describe work performed, not whether
the answer is correct or complete.
"""
from collections import Counter

from projectclarity.claim_review.review_queue import pending_candidates, MAX_REVIEW_CLAIMS, MAX_REVIEW_PASSAGES
from projectclarity.nodes.schedule_work.source_priority import source_priority, task_source_priority
from projectclarity.nodes.schedule_work.source_yield import document_yields

POLICY = 'answer-cycle-v2'
READ_QUANTUM = 4
MAX_READ_QUANTUM = 8


def reading_allowance(session, source_id, used, yields):
    """Extend useful reading one attempt at a time without starving peer service.

    Args:
        session: Run whose saved controller policy selects adaptive or legacy turns.
        source_id: Focus document, or None before one is selected.
        used: Attempts charged to this reading turn, including failed attempts.
        yields: Existing source-version-checked gain summaries in reservation order.
    Returns:
        Diagnostic dictionary with an absolute turn limit and supporting recent
        observations. V2 extends only when all three recent attempts are finalized,
        at least two earned gain and the latest earned gain. Extension is capped at
        eight attempts and reevaluated after every extra attempt. No model call,
        evidence approval, budget change or session mutation occurs.
    """
    recent = yields.get(source_id, {}).get('recent_passages', [])
    productive = (len(recent) == 3 and all(r['finalized'] for r in recent)
        and sum(r['gain'] > 0 for r in recent) >= 2 and recent[-1]['gain'] > 0)
    adaptive = session.workflow.get('controller_policy') == POLICY
    extended = adaptive and productive and READ_QUANTUM <= used < MAX_READ_QUANTUM
    return dict(source_id=source_id, used=used, base_limit=READ_QUANTUM,
        hard_limit=MAX_READ_QUANTUM if adaptive else READ_QUANTUM,
        limit=used + 1 if extended else READ_QUANTUM,
        reason='recent_productive_gain' if extended else 'fixed_turn_or_extension_not_earned',
        recent_passages=recent)


def enabled(session):
    """Check whether this run explicitly uses the designed-answer work-cycle policy.

    Require both the saved policy marker and explicit answer completion checks. Do not
    enable the new controller merely because an older session is loaded by newer code.

    Args:
        session: Run with its initialization-time policy marker and accepted schema.
    Returns:
        True for versioned designed runs with explicit requirements. Saved V1 runs
        retain fixed reading turns; V2 uses adaptive turns. Unversioned legacy runs
        continue through their old adapter even when loaded by a newer process.
    """
    return bool(session.workflow.get('controller_policy') in {POLICY, 'answer-cycle-v1'}
        and (session.answer_schema or {}).get('completion_checks'))


def choose_work(session, available):
    """Choose the next reading, sampling, review, assessment or discovery turn.

    Advance the saved phase counters while considering only tasks already permitted
    by the scheduler. The cycle prevents repeated new-search suggestions from always
    interrupting acquired documents. It updates scheduling state but does not perform
    a download, model call or file write.

    Args:
        session: Mutable run; only controller/projection state is changed here.
        available: Tasks already admitted by permissions, budgets and retry gates.
    Returns:
        A subset for one action/source, or an empty list when none is eligible.
        Four primary passage attempts form the base turn; V2 may extend it to eight
        using recent finalized gains, one extra attempt per decision. V1 retains four.
        Newly inventoried peers each receive one explicit first-read grant;
        otherwise one previously read peer receives a bounded sample. Dry sources yield earlier. Review and
        evidence-digest dependencies do not reset reading credit. Search receives one
        transaction per cycle, never an unbounded stream of gap-priority interrupts.
        Productive non-dry focus survives discovery, while peers receive samples.
        Reservations survive restart; no network, model call or file write occurs.
    """
    if not available:
        return []
    state = session.workflow.setdefault('answer_controller', dict(
        phase='read', cycle=1, read_start=0, focus_source=None, last_served={}))
    attempts = (session.extraction or {}).get('attempts', [])
    by_id = {a['batch_id']: a for a in attempts}
    served = Counter(a['source_id'] for a in attempts if a['status'] != 'not_attempted')
    total = sum(served.values())
    yields = document_yields(session)
    groups = {}
    for task in available:
        groups.setdefault(task['action'], []).append(task)
    # Check quotations from the previous extraction before using those claims
    # to choose another reading, review or discovery task.
    if groups.get('validate_evidence'):
        state['reason'] = 'Validate the evidence produced by the previous action.'
        return groups['validate_evidence']
    if session.request.budget.max_requests - session.usage.requests_attempted <= 1:
        state['reason'] = 'Use the reserved final assessment opportunity without increasing the budget.'
        return groups.get('assess_research_gaps', [])
    readings = [t for t in groups.get('extract_knowledge', []) if t.get('batch_id') in by_id]
    preparation = groups.get('retrieve_sources', []) + [t for t in groups.get('extract_knowledge', [])
        if not t.get('batch_id')]
    reviews = groups.get('review_claims', [])
    assessments = groups.get('assess_research_gaps', [])

    def source(task):
        """Find the source ID for a task, using its passage record when necessary.

        Reading priority needs a document owner, while global work such as search may
        have none. The lookup does not change the task or its evidence.

        Args:
            task: Retrieval, inventory or passage task from the enclosing projection.
        Returns:
            Source identifier, or None for source-independent work.
        """
        return task.get('source_id') or by_id.get(task.get('batch_id'), {}).get('source_id')

    def reading_tasks(sid, allowance):
        """Choose a limited set of passages from the selected source for the current turn.

        Prefer original passages over repair rereads. Record that the source was served
        before returning the choices; the outer scheduler still reserves the actual task
        and model requests before execution.

        Args:
            sid: Source selected by the cycle's durable service policy.
            allowance: Maximum remaining passage attempts in this phase.
        Returns:
            Eligible base passages before refinements, capped by phase credit.
            Updates service history before execution so an uncertain attempt is
            not forgotten on restart; normal scheduler reservation still applies.
        """
        rows = [t for t in readings if source(t) == sid]
        base = [t for t in rows if not by_id[t['batch_id']].get('refinement_parent')]
        rows = base or rows
        state['last_served'][sid] = total + 1
        state['reason'] = 'Read acquired evidence within the current service turn.'
        return rows[:max(1, allowance)]

    # At most one pass through the finite phase cycle without an executable action.
    # This is an internal transition bound, not a research request allowance.
    for _ in range(10):
        phase = state['phase']
        if phase == 'read':
            if preparation:
                state['reason'] = 'Prepare already admitted sources before choosing passages.'
                return [min(preparation, key=lambda t: (task_source_priority(session, t), source(t) or ''))]
            pending = pending_candidates(session)
            if reviews and (len(pending) >= MAX_REVIEW_CLAIMS
                    or len({c['batch_id'] for c in pending}) >= MAX_REVIEW_PASSAGES):
                state['reason'] = 'Review a full evidence batch without ending the reading turn.'
                return reviews
            used = total - state['read_start']
            sid = state.get('focus_source')
            limit = session.request.max_no_gain_passages
            dry = bool(sid and limit and yields.get(sid, {}).get('no_gain_passages', 0) >= limit)
            allowance = reading_allowance(session, sid, used, yields)
            state['reading_allocation'] = allowance
            if used >= allowance['limit'] or dry or not readings:
                state['phase'] = 'sample'
                state.pop('sample_sources', None)
                continue
            possible = {source(t) for t in readings}
            if sid not in possible:
                sid = min(possible, key=lambda candidate: (
                    bool(limit and yields.get(candidate, {}).get('no_gain_passages', 0) >= limit),
                    source_priority(session, candidate),
                    not bool(yields.get(candidate, {}).get('signal_count', 0)),
                    state['last_served'].get(candidate, 0), candidate))
                state['focus_source'] = sid
            return reading_tasks(sid, allowance['limit'] - used)
        if phase == 'sample':
            if 'sample_sources' not in state:
                peers = {source(t) for t in readings if source(t) != state.get('focus_source')}
                ordered_peers = sorted(peers, key=lambda candidate: (
                    state['last_served'].get(candidate, 0), source_priority(session, candidate), candidate))
                # Give each newly prepared source one explicit passage read, or
                # sample one previously read alternative. Fix this list now so
                # later discoveries cannot extend the sampling phase indefinitely.
                unseen = [sid for sid in ordered_peers if not served[sid]]
                state['sample_sources'] = unseen or ordered_peers[:1]
            while state['sample_sources']:
                sid = state['sample_sources'].pop(0)
                result = reading_tasks(sid, 1)
                if result:
                    state['reason'] = 'Sample an admitted source with an explicit one-passage grant.'
                    return result
            state['phase'] = 'review'
        elif phase == 'review':
            if reviews:
                state['reason'] = 'Finish available reviews for the evidence collected in this turn.'
                return reviews
            state.update(phase='assess', assessment_start=len(session.coverage.get('history', [])))
        elif phase == 'assess':
            progressed = len(session.coverage.get('history', [])) > state['assessment_start']
            reviewing = session.coverage.get('assessment_status') == 'reviewing'
            if assessments and (not progressed or reviewing):
                state['reason'] = 'Assess answer requirements, completing all current evidence batches first.'
                return assessments
            state['phase'] = 'discover'
        elif phase == 'discover':
            if groups.get('select_sources'):
                state['phase'] = 'next_turn'
                state['reason'] = 'Evaluate pending search results for one discovery transaction.'
                return groups['select_sources']
            discovery_actions = ('web_search', 'select_followup_sources')
            cursor = state.get('discovery_cursor', 0)
            for offset in range(len(discovery_actions)):
                index = (cursor + offset) % len(discovery_actions)
                action = discovery_actions[index]
                if groups.get(action):
                    state['discovery_cursor'] = (index + 1) % len(discovery_actions)
                    state['phase'] = 'select' if action == 'web_search' else 'next_turn'
                    state['reason'] = 'Take one discovery turn after servicing acquired evidence.'
                    return groups[action]
            state['phase'] = 'next_turn'
        elif phase == 'select':
            state['phase'] = 'next_turn'
            if groups.get('select_sources'):
                state['reason'] = 'Finish this search transaction, then return to acquired evidence.'
                return groups['select_sources']
        elif phase == 'next_turn':
            # Keep the document chosen for useful reading across a discovery
            # turn. The next reading phase reselects it if it has no eligible
            # passages or has reached the configured no-gain limit.
            sid = state.get('focus_source')
            gain = yields.get(sid, {})
            limit = session.request.max_no_gain_passages
            retain = (sid in {source(t) for t in readings} and gain.get('signal_count', 0)
                      and not (limit and gain.get('no_gain_passages', 0) >= limit))
            state.update(phase='read', cycle=state['cycle'] + 1, read_start=total,
                         focus_source=sid if retain else None)
            if groups.get('accumulate_knowledge'):
                state['reason'] = 'Service permitted auxiliary capture between research turns.'
                return groups['accumulate_knowledge']
        else:
            raise ValueError('unknown_answer_controller_phase')
    # No eligible primary work survived this cycle. Do not resurrect tasks or
    # silently delegate back to legacy selectors.
    return []
