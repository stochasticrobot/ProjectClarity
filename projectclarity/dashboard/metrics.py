"""Calculate and retain the counts shown in research progress displays.

The dashboard distinguishes named entities extracted from text, report entries
proposed for a goal, claims and model tokens. These are different quantities: ten
claims can describe one person. Helpers group labels for display, reconcile saved
token usage, and append changed measurements when a session is saved. Missing
usage remains unknown rather than being counted as zero. No model is called and
display grouping does not establish that two names identify the same real entity.
"""
import hashlib
import json
import time
from collections import Counter
from projectclarity.dashboard.targets import target_counts


def label_key(name, kind):
    """Create a stable display-group key without guessing aliases or identity.

    Args:
        name: Observed name or structured item label.
        kind: Observed entity type, not inferred from the research question.
    Returns:
        Short hash of case/whitespace-normalized labels; homonyms can share a group.
    """
    return hashlib.sha256(json.dumps([' '.join(str(x).casefold().split())
        for x in (name, kind)], ensure_ascii=False).encode()).hexdigest()[:24]


def entity_groups(data):
    """Collect every extracted entity and otherwise-unrepresented result item.

    Args:
        data: Serialized session with extraction attempts and candidate claims.
    Returns:
        Display groups with explicit mention references, claims and relationships.
        Unknown result-item types stay unspecified; no alias resolution or factual
        promotion occurs. Repeated exact labels are grouped, never called identities.
    """
    extraction = data.get('extraction') or {}
    groups, local = {}, {}
    for attempt in extraction.get('attempts', []):
        for entity in (attempt.get('result') or {}).get('entities', []):
            key = label_key(entity['name'], entity['entity_type'])
            row = groups.setdefault(key, dict(id=key, name=entity['name'],
                type=entity['entity_type'], mentions=[], claim_ids=[], relationships=[]))
            ref = dict(batch_id=attempt['batch_id'], source_id=attempt['source_id'],
                       local_id=entity['entity_id'])
            if ref not in row['mentions']:
                row['mentions'].append(ref)
            local[(attempt['batch_id'], entity['entity_id'])] = key
    by_name = {}
    for key, row in groups.items():
        by_name.setdefault(' '.join(row['name'].casefold().split()), []).append(key)
    for candidate in extraction.get('candidates', []):
        proposal = candidate['proposal']
        related = {local[(candidate['batch_id'], eid)] for eid in proposal['entity_ids']
                   if (candidate['batch_id'], eid) in local}
        for record in proposal.get('result_records', []):
            fields = record['fields']
            # Only an explicitly named field supplies a name. Arbitrary shapes
            # remain visible as structured item labels, not guessed people.
            name = next((f['value'] for f in fields if f['name'] in
                {'name', 'full_name', 'member_name', 'title'}), None)
            matches = by_name.get(' '.join((name or '').casefold().split()), [])
            if len(matches) == 1:
                related.add(matches[0])
            else:
                display = name or '; '.join(f"{f['name']}: {f['value']}" for f in fields)
                if not display:
                    continue
                key = label_key(display, 'result item (type unspecified)')
                row = groups.setdefault(key, dict(id=key, name=display,
                    type='result item (type unspecified)', mentions=[], claim_ids=[], relationships=[]))
                ref = dict(batch_id=candidate['batch_id'], source_id=candidate['source_id'], local_id=None)
                if ref not in row['mentions']:
                    row['mentions'].append(ref)
                related.add(key)
        relation = proposal.get('relationship')
        if relation:
            subject = local.get((candidate['batch_id'], relation['subject_id']))
            target = local.get((candidate['batch_id'], relation['object_id']))
            if subject and target:
                edge = dict(subject=subject, predicate=relation['predicate'], object=target,
                            claim_id=candidate['candidate_id'])
                for key in {subject, target}:
                    if edge not in groups[key]['relationships']:
                        groups[key]['relationships'].append(edge)
        for key in related:
            if candidate['candidate_id'] not in groups[key]['claim_ids']:
                groups[key]['claim_ids'].append(candidate['candidate_id'])
    return list(groups.values())


def token_usage(data):
    """Reconcile reported token receipts without counting retries twice or inventing zeros.

    Args:
        data: Serialized session, including wire-attempt ledger and legacy call receipts.
    Returns:
        Input/output/total, per-task reported subtotals, completeness and unknown-call
        counts. Complete per-attempt receipts take precedence over older aggregates. Missing usage is
        a lower bound, not zero actual consumption; cache tokens are not added twice.
    """
    receipts = {a['batch_id']: a for a in (data.get('extraction') or {}).get('attempts', [])}
    receipts.update({c['call_id']: c for c in data.get('processing_calls', [])})
    ledger = data.get('model_attempts', [])
    totals = Counter(input=0, output=0, total=0)
    by_task, known, missing, active = {}, 0, 0, 0
    for attempt in ledger:
        usage = attempt.get('token_usage')
        if usage is None and attempt['status'] == 'response_received':
            usage = receipts.get(attempt['call_id'], {})
        usage = usage or {}
        if all(isinstance(usage.get(k + '_tokens'), int) for k in ('input', 'output', 'total')):
            known += 1
            row = by_task.setdefault(attempt['task'], dict(input=0, output=0, total=0, calls=0))
            row['calls'] += 1
            for key in ('input', 'output', 'total'):
                value = usage[key + '_tokens']
                totals[key] += value
                row[key] += value
        elif attempt['status'] == 'started':
            active += 1
        else:
            missing += 1
    aggregate = data.get('usage', {})
    calls = max(aggregate.get('requests_attempted', 0), len(ledger))
    missing = max(missing, calls - known - active)
    aggregate_known = all(isinstance(aggregate.get(k + '_tokens'), int) for k in totals)
    # save_session can run after the response receipt but before the caller updates
    # cumulative usage. Never replace newer receipts with a lagging aggregate.
    use_aggregate = aggregate_known and missing > 0 and all(
        aggregate[k + '_tokens'] >= totals[k] for k in totals)
    if use_aggregate:
        totals = {k: aggregate[k + '_tokens'] for k in totals}
    all_responses = all(a['status'] == 'response_received' for a in ledger)
    complete = active == 0 and (missing == 0 or (use_aggregate and all_responses))
    return dict(**totals, complete=complete,
        known_calls=known, missing_calls=missing, active_calls=active,
        calls=calls,
        limit=data['request']['budget']['max_requests'], by_task=by_task,
        breakdown_complete=missing == 0 and active == 0,
        basis='reported aggregate' if use_aggregate else 'reported call subtotal')


def measurement(data):
    """Collect the counts and usage values displayed for the current saved run.

    Keep named-entity groups, proposed goal entries and reviewed statements separate.
    A supported statement about a person is not automatically a fully qualified
    person entry in the answer.

    Args:
        data: Serialized session at a persistence boundary.
    Returns:
        Compact counts and usage for a live chart, including per-goal target groups.
        Entity verification remains null; target support uses complete answer records,
        not globally resolved identities or a separate eligibility judgment.
    """
    candidates = (data.get('extraction') or {}).get('candidates', [])
    supported = {c['candidate_id'] for c in candidates
                 if c['adjudication_status'] == 'supported' and c['evidence_status'] == 'anchored'}
    entities = entity_groups(data)
    return dict(entity_labels=len(entities), candidate_claims=len(candidates),
        supported_claims=len(supported), supported_entity_labels=sum(
            bool(supported.intersection(e['claim_ids'])) for e in entities),
        goal_verified_entities=None, targets=target_counts(data), tokens=token_usage(data))


_LAST = {}


def record_measurement(data, directory):
    """Append changed measurements at real session saves, independently of browser polling.

    Args:
        data: JSON-compatible current session.
        directory: This run's existing directory; never another run's store.
    Returns:
        None. Append a timestamped sample only when measured values change. Older
        runs are not backfilled. Restart compares the last saved sample. I/O errors
        propagate to the caller, which decides whether UI telemetry is optional.
    """
    path = directory / 'dashboard_metrics.jsonl'
    key = str(path.resolve())
    current = measurement(data)
    if key not in _LAST:
        previous = None
        if path.exists():
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 32768))
                lines = stream.read().splitlines()
            if lines:
                try:
                    previous = json.loads(lines[-1]).get('values')
                except ValueError:
                    pass
        _LAST[key] = previous
    if current == _LAST[key]:
        return
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(timestamp=time.time(), values=current)) + '\n')
    _LAST[key] = current
    if len(_LAST) > 128:
        _LAST.pop(next(iter(_LAST)))
