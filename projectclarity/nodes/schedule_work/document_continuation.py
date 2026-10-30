"""Prefer further reading of a document that is producing useful answer records.

For the older scheduling policy, these helpers identify sources that have supplied
usable fields for a requested list item or other answer evidence. They prefer more
base passages from those sources while allowing periodic reads of another source
and review of waiting claims. For example, a directory yielding researcher entries
can keep attention without permanently excluding another directory. A background
claim alone does not establish list-item progress. The scheduler still controls
budgets, retries and execution; these helpers only choose among eligible tasks.
"""
from collections import Counter

from projectclarity.research_results import record_check
from projectclarity.result_identity import result_key, person_labels, organization_labels
from projectclarity.nodes.schedule_work.source_priority import source_priority, task_source_priority

MAX_FOCUSED_PASSAGES = 4


def answer_priority(proposal, goals):
    """Find the earliest research goal for which a proposed claim offers usable reading guidance.

    For list-producing goals, require actual nonempty result fields rather than a
    background statement tagged as answer evidence. A proposed item can guide more
    reading while still awaiting support review; this priority does not approve it.

    Args:
        proposal: Original serialized claim; never rewritten or interpreted as truth.
        goals: Ordered typed plan goals, optionally defining enumeration result shapes.
    Returns:
        Lowest applicable goal index, or None when the candidate cannot establish
        document focus. Shaped goals require nonempty, non-invalid item fields.
        Explicit item leads may guide reading without becoming supported results.
        Unshaped goals retain answer-evidence continuation. Counterevidence alone never
        establishes positive enumeration progress; dual-role answer evidence can. No model call or mutation occurs.
    """
    if ('counterevidence' in proposal.get('research_roles', [])
            and 'answer_evidence' not in proposal.get('research_roles', [])):
        return None
    for index, goal in enumerate(goals):
        if goal.goal_id not in proposal.get('goal_ids', []):
            continue
        if getattr(goal, 'result_shape', None) is None:
            if 'answer_evidence' in proposal.get('research_roles', []):
                return index
            continue
        for record in proposal.get('result_records', []):
            if record.get('goal_id') != goal.goal_id:
                continue
            checked = record_check(record, goal.model_dump())
            if checked['status'] != 'invalid' and any(v.strip() for v in checked['values'].values()):
                return index
    return None


def document_work(session, available):
    """Choose continued reading of a productive source, with periodic review and other-source sampling.

    A productive source has supplied usable answer information for a plan goal. After
    a limited number of focused passage attempts, offer another ready source a sample
    read. Choose only from the scheduler's already eligible tasks and preserve the
    focus decision across restarts.

    Args:
        session: Mutable run with current documents, candidate reviews and focus journal.
        available: Ready scheduler tasks after resource, attempt and recovery checks.
    Returns:
        A restricted ready list for the focused source, one peer probe, or queued
        review/validation. None leaves ordinary scheduling active. After four focused
        passage attempts, offer one ready base passage from another source; review
        may run first, and the probe reservation survives restart. Better-priority
        goal evidence can replace a secondary focus. For equal goals, official
        domains precede item yield; first official probes interrupt secondary focus. Base work precedes refinements;
        all original claims, failed attempts and deferred work remain untouched.
        Empty later passages do not erase a valid signal. No budget is replenished.
    """
    extraction = session.extraction or {}
    people = person_labels(extraction)
    organizations = organization_labels(extraction)
    attempts = extraction.get('attempts', [])
    documents = {d.source_id: d for d in session.documents if d.status == 'retrieved'}
    goals = session.plan.goals if session.plan else []
    signals, priorities, items = {}, {}, {}
    for candidate in extraction.get('candidates', []):
        doc = documents.get(candidate['source_id'])
        priority = answer_priority(candidate.get('proposal', {}), goals)
        if (doc is None or not doc.text_sha256 or priority is None
                or candidate.get('text_sha256') != doc.text_sha256
                or candidate.get('evidence_status') != 'anchored' or not candidate.get('evidence')
                or candidate.get('adjudication_status', 'pending') not in {'pending', 'supported', 'needs_review'}):
            continue
        key = (doc.source_id, doc.text_sha256)
        signals.setdefault(key, []).append(candidate['candidate_id'])
        priorities[key] = min(priorities.get(key, priority), priority)
        goal = goals[priority]
        if getattr(goal, 'result_shape', None) is not None:
            for record in candidate['proposal'].get('result_records', []):
                if record.get('goal_id') == goal.goal_id:
                    check = record_check(record, goal.model_dump())
                    if check['status'] != 'invalid' and check['values']:
                        items.setdefault((key, priority), set()).add(result_key(check['values'], people, organizations))

    pending = {}
    by_id = {a['batch_id']: a for a in attempts}
    served = Counter((a['source_id'], a['text_sha256']) for a in attempts if a['status'] != 'not_attempted')
    base_served = Counter((a['source_id'], a['text_sha256']) for a in attempts
        if a['status'] != 'not_attempted' and not a.get('refinement_parent'))
    for attempt in attempts:
        if attempt['status'] == 'not_attempted':
            pending.setdefault((attempt['source_id'], attempt['text_sha256']), set()).add(attempt['batch_id'])
    reviews = {}
    if session.request.review_claims:
        for candidate in extraction.get('candidates', []):
            if candidate['evidence_status'] == 'anchored' and candidate['candidate_id'] not in session.claim_reviews:
                reviews.setdefault((candidate['source_id'], candidate['text_sha256']), []).append(candidate['candidate_id'])

    focus = session.workflow.get('document_focus', {})
    key = (focus.get('source_id'), focus.get('text_sha256'))
    alternatives = [k for k in signals if pending.get(k)]
    def rank(source_key):
        """Prefer earlier goals, official institutions, then observed item yield.

        Args:
            source_key: Source ID and current text hash with a qualifying signal.
        Returns:
            Scheduling rank only, not a quality or completeness judgment. Canonical goal-field
            keys deduplicate repeat claims; failed attempts still cost effort.
        """
        priority = priorities[source_key]
        return priority, source_priority(session, source_key[0]), -len(items.get((source_key, priority), ())) / max(1, base_served[source_key])

    best = min(alternatives, key=rank) if alternatives else None
    if focus.get('status') in {'reading', 'reviewing', 'blocked'}:
        if key not in signals or not (pending.get(key) or reviews.get(key)):
            rows = [a for a in attempts if (a['source_id'], a['text_sha256']) == key]
            reason = ('signal_rejected_or_source_changed' if key not in signals else
                      'all_passages_completed' if rows and all(a['status'] == 'completed' for a in rows)
                      else 'passages_exhausted_with_errors')
            focus.update(status='released', release_reason=reason,
                         remaining_passages=len(pending.get(key, ())), pending_reviews=len(reviews.get(key, ())))
        elif best is not None and rank(best) < rank(key):
            focus.update(status='released', release_reason=('official_source_priority'
                if priorities[best] == priorities[key] and source_priority(session, best[0]) < source_priority(session, key[0])
                else 'higher_priority_item_yield'))

    if focus.get('status') not in {'reading', 'reviewing', 'blocked'}:
        key = best
        if key is None:
            return None
        if focus:
            session.workflow.setdefault('document_focus_history', []).append(dict(focus))
        focus = {'source_id': key[0], 'text_sha256': key[1], 'status': 'reading',
                 'started_model_requests': session.usage.requests_attempted, 'release_reason': None,
                 'last_probe_count': served[key]}
        session.workflow['document_focus'] = focus

    focus['candidate_ids'] = signals[key]
    focus['remaining_passages'] = len(pending.get(key, ()))
    focus['pending_reviews'] = len(reviews.get(key, ()))
    reading = [t for t in available if t['action'] == 'extract_knowledge'
               and t.get('batch_id') in pending.get(key, ())]
    base = [t for t in reading if not by_id[t['batch_id']].get('refinement_parent')]
    reading = base or reading
    reviewing = [t for t in available if t['action'] == 'review_claims']
    validating = [t for t in available if t['action'] == 'validate_evidence']
    # A source cannot compete until it has been acquired and inventoried. Do not
    # hide these finite, already-registered inputs behind a productive focus.
    preparation = [t for t in available if
        (t['action'] == 'retrieve_sources' and t.get('source_id') and t['source_id'] != key[0])
        or (t['action'] == 'extract_knowledge' and not t.get('batch_id'))]
    if preparation:
        preparation.sort(key=lambda task: task_source_priority(session, task))
        return [preparation[0]] + reviewing + validating
    probe_id = focus.get('probe_batch_id')
    if probe_id and by_id.get(probe_id, {}).get('status') != 'not_attempted':
        focus['last_probe_count'] = served[key]
        focus.pop('probe_batch_id', None)
        probe_id = None
    peers = [t for t in available if t['action'] == 'extract_knowledge'
        and t.get('batch_id') in by_id and by_id[t['batch_id']]['source_id'] != key[0]
        and not by_id[t['batch_id']].get('refinement_parent')]
    peers.sort(key=lambda task: task_source_priority(session, task))
    official_probe = next((t for t in peers if task_source_priority(session, t) == 0
        and base_served[(by_id[t['batch_id']]['source_id'], by_id[t['batch_id']]['text_sha256'])] == 0), None)
    if official_probe and source_priority(session, key[0]) > 0:
        focus['probe_batch_id'] = official_probe['batch_id']
        return [official_probe] + reviewing + validating
    focus.setdefault('last_probe_count', served[key])
    if reading and peers and (probe_id or served[key] - focus['last_probe_count'] >= MAX_FOCUSED_PASSAGES):
        probe = next((t for t in peers if t['batch_id'] == probe_id), peers[0])
        focus['probe_batch_id'] = probe['batch_id']
        focus['status'] = 'reading'
        return [probe] + reviewing + validating
    if reading:
        focus['status'] = 'reading'
        return reading + reviewing + validating
    if reviews.get(key) and reviewing:
        focus['status'] = 'reviewing'
        return reviewing + validating
    focus['status'] = 'blocked'
    return None
