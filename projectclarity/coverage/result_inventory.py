"""Provide report-entry counts and evidence links to coverage assessment.

Reports and the dashboard already group extracted fields into result records,
such as one researcher with a name and affiliation. coverage_results supplies that
same inventory to the model assessing research completion. align_result_items
then connects the model's proposed list items to actual records through goal IDs
and evidence references, rather than treating a new label as a new result.

These helpers do not recount prose or decide whether the evidence is sufficient.
They use the source-checked candidates supplied by the caller and perform no I/O.
"""
from projectclarity.research_results import result_progress


def coverage_results(session, valid):
    """Build per-goal report inventories for the model assessing answer completion.

    Use the same entry grouping and statuses as the report and dashboard, retaining
    unassigned observations and missing-contribution records. This prevents the model
    from receiving a different result count merely because it is assessing coverage.

    Args:
        session: Typed run whose plan defines optional result shapes.
        valid: Freshly source-checked candidate mapping; no previous runs are read.
    Returns:
        Goal-keyed ledgers containing supported, pending, disputed, identity-pending
        and incomplete entries, plus unassigned observations, mapping gaps and
        candidate references. Invalid rows are counted but omitted from items.
        Values are copied from the shared inventory; no evidence is manufactured,
        state changed or I/O performed.
    """
    progress = result_progress(session.model_dump(mode='json'), set(valid))
    return {g['goal_id']: dict(unit=g['results']['unit'],
        count_basis=g['results']['count_basis'],
        required_fields=g['results']['required_fields'],
        identified=g['results']['identified'], supported=g['results']['supported'],
        pending=g['results']['pending'], disputed=g['results']['disputed'],
        identity_pending=g['results']['identity_pending'],
        incomplete=g['results']['incomplete'], invalid=g['results']['invalid'],
        mapping_gaps=[r for r in progress['mapping_ledger']['rows'] if r['goal_id'] == g['goal_id'] and r['blocking']],
        unassigned_count=g['results'].get('unassigned_count', 0),
        unassigned_observations=g['results'].get('unassigned_observations', []),
        items=[dict(result_id=r['result_id'], fields=r['values'],
                    display_values=r.get('display_values', {}), status=r['status'],
                    field_support_status=r.get('field_support_status', r['status']),
                    qualification_status=r.get('qualification_status', 'not_assessed_legacy'),
                    qualification_requirements=r.get('qualification_requirements', []),
                    candidate_ids=r['candidate_ids'], field_status=r.get('field_status')) for r in g['results']['records']
               if r['status'] in {'supported', 'pending', 'disputed', 'identity_pending', 'incomplete'}])
        for g in progress['goals'] if g['results'] is not None}


def align_result_items(criterion, assessment, ledgers):
    """Resolve a coverage reply's proposed list items to actual saved report records.

    Use an explicit matching result goal when present; older replies must map each
    item's candidate references unambiguously to one record. Model-written labels
    alone cannot create new entries or establish their identity.

    Args:
        criterion: One plan criterion; combined/supplemental units are not inferred.
        assessment: Parsed assessment with evidence-linked proposed items.
        ledgers: Complete current goal ledgers, not the selected evidence subset.
    Returns:
        Goal ID and mapped result IDs after explicit result_goal_id alignment or
        unambiguous legacy item-to-record mapping. Explicit alignment uses the
        code-counted supported inventory; legacy empty items establish nothing.
        Wrong declared goal IDs raise ValueError. An inapplicable criterion, absent
        ledger, empty legacy item list or ambiguous candidate mapping returns
        (None, []). Duplicate resolved IDs are returned unchanged for the caller
        to reject; this function does not silently deduplicate enumeration items.
    """
    if criterion.kind != 'enumeration' or len(criterion.goal_ids) != 1:
        return None, []
    gid = criterion.goal_ids[0]
    ledger = ledgers.get(gid)
    if not ledger:
        return None, []
    if assessment.result_goal_id is not None:
        if assessment.result_goal_id != gid:
            raise ValueError('result_goal_unit_mismatch')
        return gid, [item['result_id'] for item in ledger['items'] if item['status'] == 'supported']
    if not assessment.items:
        return None, []
    by_candidate = {}
    for item in ledger['items']:
        for cid in item['candidate_ids']:
            by_candidate.setdefault(cid, set()).add(item['result_id'])
    resolved = []
    for item in assessment.items:
        matches = set().union(*(by_candidate.get(cid, set()) for cid in item.candidate_ids))
        if len(matches) != 1:
            return None, []
        resolved.append(next(iter(matches)))
    return gid, resolved
