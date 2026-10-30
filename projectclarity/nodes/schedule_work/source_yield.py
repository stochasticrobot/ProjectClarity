"""Measure new records and evidence improvements produced by each source passage.

Scheduling uses these measurements to decide whether to keep reading one document
or try another. A gain can be a distinct result for a goal or a useful improvement
to existing evidence; another repetition of the same claim is not automatically a
gain. Consecutive successful passages without gains form the document's no-gain
count. Failed reads remain failures rather than empty evidence. The calculations
are limited to the supplied run and do not establish research completeness.
"""
from collections import defaultdict
from urllib.parse import urlsplit

from projectclarity.research_results import record_check, structure_status
from projectclarity.result_identity import normalized, result_key, person_labels, organization_labels


def passage_gains(session):
    """Count new goal records and useful evidence improvements attributable to each passage.

    Scheduling needs to distinguish fresh information from repeated mentions of the
    same item. Group explicit result fields for list goals and track improvements in
    support; other goals use statement and qualification keys. The counts guide
    reading order, not truth or completion decisions.

    Args:
        session: Current run with anchored candidates and source-version receipts.
    Returns:
        Batch-ID mapping of new-record/evidence-improvement counts. List goals use normalized
        item fields, not claim mentions; incomplete records remain candidate leads.
        First usable support and support on another host count separately. Hosts
        are not proof of independence. Counterevidence and coverage claims count
        by normalized statement/qualifications; unshaped goals also use that proxy.
        Failed/stale/unanchored claims do not earn gain. No truth, completeness,
        semantic deduplication, model call or session mutation is implied.
    """
    goals = {g.goal_id: g.model_dump() for g in session.plan.goals} if session.plan else {}
    docs = {d.source_id: d for d in session.documents if d.status == 'retrieved'}
    sources = {s.source_id: s for s in session.sources}
    seen, supported, corroborated = set(), set(), set()
    people = person_labels(session.extraction)
    organizations = organization_labels(session.extraction)
    gains = defaultdict(int)
    for c in (session.extraction or {}).get('candidates', []):
        doc = docs.get(c['source_id'])
        if (not doc or c.get('text_sha256') != doc.text_sha256
                or c.get('evidence_status') != 'anchored' or not c.get('evidence')
                or c.get('validation_errors') or c.get('adjudication_status') in
                {'invalid_evidence', 'out_of_scope', 'contradicted'}):
            continue
        claim = c['proposal']
        roles = set(claim.get('research_roles', []))
        verdict = c.get('adjudication_status', 'pending')
        batch = c['batch_id']
        source = sources.get(c['source_id'])
        host = urlsplit(getattr(doc, 'final_url', None) or getattr(source, 'url', '')).hostname or c['source_id']
        keys = []
        for gid in claim.get('goal_ids', []):
            goal = goals.get(gid)
            if not goal:
                continue
            if roles & {'counterevidence', 'coverage_evidence'} or (
                    not goal.get('result_shape') and roles & {'answer_evidence', 'lead'}):
                if verdict != 'contradicted' or 'counterevidence' in roles:
                    keys.append((('claim', gid, normalized(claim['statement']),
                        tuple(sorted(normalized(q) for q in claim.get('qualifications', [])))), True))
            if not goal.get('result_shape') or verdict == 'contradicted':
                continue
            for record in claim.get('result_records', []):
                if record.get('goal_id') != gid:
                    continue
                check = record_check(record, goal)
                values = result_key({k: v for k, v in check['values'].items() if v.strip()}, people, organizations)
                if check['status'] != 'invalid' and values:
                    keys.append((('item', gid, values), check['status'] == 'complete'))
        for key, complete in keys:
            if key not in seen:
                gains[batch] += 1
                seen.add(key)
            usable = (verdict == 'supported' and complete and
                (key[0] != 'item' or ('answer_evidence' in roles
                 and structure_status(claim) in {'structured', 'non_relational'})))
            if usable:
                if key not in supported:
                    supported.add(key)
                    gains[batch] += 1
                if (key, host) not in corroborated:
                    corroborated.add((key, host))
                    gains[batch] += 1
    return dict(gains)


def document_yields(session):
    """Count recent completed passages that added no new record or evidence improvement.

    Work since the last source switch is considered in execution order. Failed or
    unfinished attempts break the no-gain streak rather than pretending the passage
    was successfully read and empty.

    Args:
        session: Run with finalized extraction batches and saved rotation watermarks.
    Returns:
        Source-ID summaries with a conservative no-gain streak. Failed, uncertain
        and unfinalized attempts break a streak, never count as empty evidence.
        Review upgrades are recomputed from retained evidence; a later supported
        duplicate can therefore reset a tail. No receipt or queued passage changes.
    """
    gains = passage_gains(session)
    finalized = set(session.workflow.get('finalized_batches', []))
    resets = session.workflow.get('yield_resets', {})
    summaries = {}
    order = {a['call_id']: i for i, a in enumerate(getattr(session, 'model_attempts', []))
        if a.get('task') == 'extract_knowledge'}
    for doc in session.documents:
        if doc.status != 'retrieved':
            continue
        rows = [a for a in (session.extraction or {}).get('attempts', [])
            if a['source_id'] == doc.source_id and a['text_sha256'] == doc.text_sha256
            and a['status'] != 'not_attempted']
        # Saved passage order is not reading order: section samples and repair
        # rereads can jump ahead. Model-request reservation order also
        # keeps concurrent completions from making the result nondeterministic.
        rows.sort(key=lambda a: order.get(a['batch_id'], -1))
        reset = resets.get(doc.source_id, {})
        start = reset.get('attempted', 0) if reset.get('text_sha256') == doc.text_sha256 else 0
        streak = 0
        for row in rows[start:]:
            if row['status'] != 'completed' or row['batch_id'] not in finalized or gains.get(row['batch_id'], 0):
                streak = 0
            else:
                streak += 1
        summaries[doc.source_id] = {'text_sha256': doc.text_sha256, 'attempted': len(rows),
            'no_gain_passages': streak, 'signal_count': sum(gains.get(a['batch_id'], 0) for a in rows)}
    return summaries
