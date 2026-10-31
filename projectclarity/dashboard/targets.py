"""Prepare the report entries and estimated totals shown in the Targets display.

A target here is an item proposed for a research goal, such as one researcher in
a requested list. Helpers reuse the same result inventory as reports and coverage
assessment, then attach statuses, field variants and recorded scope estimates.
They do not count every named entity in a document as a target. Estimates remain
estimates and cannot establish completion percentages or stop research. These
functions process supplied run data without model calls or file writes.
"""
from projectclarity.result_identity import NAME_FIELDS, normalized

from projectclarity.research_results import result_progress


def target_groups(progress):
    """Project only records explicitly attached to an enumeration goal into targets.

    Args:
        progress: Result inventory, optionally source-integrity checked by the caller.
    Returns:
        One display row per shared goal-result identity, with retained field
        variants and evidence. No name-only regrouping occurs here. Disputed
        entries retain one identity; unassigned observations are a separate collection
        and do not count as report records. Incomplete identified entries remain rows.
        Source support is not independent truth verification. No mutation occurs.
    """
    goals = []
    for goal in progress.get('goals', []):
        if goal.get('report_section') is False:
            continue
        inventory = goal.get('results')
        if inventory is None:
            continue
        fields = inventory['required_fields']
        names = [f for f in fields if f.casefold() in NAME_FIELDS]
        name_field = names[0] if len(names) == 1 else None
        rows = []
        for record in inventory['records']:
            if record['status'] not in {'supported', 'pending', 'disputed', 'identity_pending', 'incomplete'}:
                continue
            if record['status'] == 'incomplete' and inventory['count_basis'] != 'answer-schema-v1':
                continue
            values = record['values']
            rows.append(dict(id=record['result_id'],
                label=values.get(name_field, 'Unidentified entry') if name_field else ' · '.join(values.get(f, 'Unknown') for f in fields),
                records=[record], candidate_ids=record['candidate_ids'],
                qualifications=record['qualifications'] + ([record['identity_note']] if record.get('identity_note') else []), status=record['status'],
                variants=len(record['field_variants'])))
        rows.sort(key=lambda r: normalized(r['label']))
        goals.append(dict(goal_id=goal['goal_id'], question=goal['question'], unit=inventory['unit'],
            fields=fields, name_field=name_field, count_basis=inventory['count_basis'],
            qualification_policy=inventory.get('qualification_policy', 'legacy_field_support'),
            field_supported=inventory.get('field_supported', inventory['supported']),
            measurement=goal.get('measurement', {}),
            identified=inventory['identified'], supported=inventory['supported'],
            pending=inventory['pending'], disputed=inventory['disputed'], identity_pending=inventory['identity_pending'],
            incomplete=inventory['incomplete'], invalid=inventory['invalid'],
            without_records=inventory['claims_without_records'],
            unassigned_observations=inventory.get('unassigned_observations', []), rows=rows))
    return goals


def target_counts(data):
    """Measure goal-specific display targets at actual session-save boundaries.

    Args:
        data: One serialized session; recorded anchor statuses are used for telemetry.
    Returns:
        Counts keyed by goal ID, never summed across different units. Live API
        projections independently recheck source evidence before displaying counts.
    """
    return {g['goal_id']: {k: g[k] for k in
            ('identified', 'field_supported', 'supported', 'pending', 'disputed', 'identity_pending', 'count_basis', 'qualification_policy')}
            for g in target_groups(result_progress(data))}


def scope_summary(data):
    """Select the latest recorded population estimate for each plan goal.

    Args:
        data: Serialized session with optional initial-plan, early-study and coverage
            estimates. The input and its persisted receipts are not modified.
    Returns:
        Estimate dictionaries in plan-goal order, annotated with origin and goal_id.
        Coverage estimates take precedence once any coverage-history entry succeeded;
        study estimates override initial priors otherwise. Unknown bounds remain
        unknown. The UI may display these beside entry counts, but they do not become
        exact percentage denominators or stopping targets. No evidence is revalidated
        here, so a displayed estimate may be older than the latest collected claims."""
    plan, coverage = data.get('plan') or {}, data.get('coverage') or {}
    layers = [('initial plan', plan.get('scope_estimates', [])),
              ('scope study', (data.get('coverage_study') or {}).get('estimates', []))]
    if any(h.get('status') == 'assessed' for h in coverage.get('history', [])):
        layers.append(('coverage review', coverage.get('scope_estimates', [])))
    chosen = {}
    for origin, estimates in layers:
        for estimate in estimates:
            for goal in estimate.get('goal_ids', []):
                chosen[goal] = {**estimate, 'origin': origin, 'goal_id': goal}
    return [chosen[g['goal_id']] for g in plan.get('goals', []) if g['goal_id'] in chosen]
