"""Calculate result counts and, when justified, percentages for one research goal.

A research goal is one question within a plan, such as listing researchers at a
university. Reporting code supplies the records found for that goal. This module
copies their identified, supported, pending and disputed counts, then determines
whether there is a suitable total to divide by.

A total is accepted from an explicit user-requested count or from a coverage
assessment tied to supported source claims for that same goal. For example,
10 identified researchers out of a supported total of 20 can be shown as 50%.
An estimate of 10–30 researchers cannot supply that percentage. Conflicting or
missing totals leave percentages unknown; counts above the total are flagged.
Goals without a list of records, such as explaining a cause, return the status of
their evidence requirements instead. No model calls, file access or state changes
occur here.
"""


def measure_goal(data, goal, inventory, valid_ids=None):
    """Calculate one goal's result counts and a justified completion denominator.

    Use a user-requested total or a supported, goal-aligned assessment total, never
    an initial estimate. For a goal without record-based results, expose its evidence
    checks instead of inventing a number of things to find.

    Args:
        data: Serialized session containing the plan, coverage assessments and
            extracted candidates. No other runs or source files are read.
        goal: Serialized plan goal with goal_id; only criteria naming that goal alone
            are considered for its checks and target count.
        inventory: Result-count dictionary for that goal, or None when the goal has
            no record inventory, for example an explanation rather than a list.
        valid_ids: Optional set of candidate IDs freshly checked against source files.
            When supplied, only those candidates can support an assessed target.
            None trusts the candidates' saved quotation-validation status instead.

    Returns:
        A new dictionary with result counts or evidence-check statuses, the accepted
        target and its source, and identified/supported percentages. Missing or
        conflicting targets leave percentages as None. A recorded unverified
        population cannot become a display denominator. Legacy rows without this
        receipt retain the existing evidence checks. A zero target also has no
        percentage. Counts exceeding the target are flagged, not capped at 100%.
        No supplied data is changed and no model or file operation is performed.
    """
    criteria = [c for c in (data.get('plan') or {}).get('completion_criteria', [])
                if c.get('goal_ids') == [goal['goal_id']]]
    rows = {c['criterion_id']: c for c in (data.get('coverage') or {}).get('criteria_progress', [])}
    checks = [dict(id=c['criterion_id'], description=c['description'],
        status=rows.get(c['criterion_id'], {}).get('status', 'not_assessed')) for c in criteria]
    measurement = dict(kind='results' if inventory is not None else 'evidence_checks',
        target=None, target_basis='unknown', target_candidate_ids=[],
        identified=inventory['identified'] if inventory else None,
        supported=inventory['supported'] if inventory else None,
        pending=inventory['pending'] if inventory else None,
        disputed=inventory['disputed'] if inventory else None,
        identity_pending=inventory['identity_pending'] if inventory else None,
        found_percent=None, supported_percent=None, target_exceeded=False, checks=checks)
    if inventory is None:
        return measurement
    candidates = {c['candidate_id']: c for c in (data.get('extraction') or {}).get('candidates', [])
        if c.get('evidence_status') == 'anchored' and (valid_ids is None or c['candidate_id'] in valid_ids)}
    targets = []
    for criterion in criteria:
        if criterion['kind'] != 'enumeration':
            continue
        if criterion['target_basis'] == 'user_request' and criterion.get('target_count') is not None:
            targets.append((criterion['target_count'], 'user_request', []))
            continue
        row = rows.get(criterion['criterion_id'], {})
        refs = row.get('benchmark_candidate_ids', [])
        # Mapping protects against interpreting a seat/vacancy count as a count
        # of people, or a relationship count as a count of organizations.
        aligned = row.get('result_goal_id') == goal['goal_id']
        population = row.get('population')
        if (row.get('target_count') is not None and aligned and refs and
                (population is None or population.get('state') == 'model_asserted_supported_benchmark') and
                all(cid in candidates and candidates[cid].get('adjudication_status') == 'supported'
                    for cid in refs)):
            targets.append((row['target_count'], 'source_backed_assessment', refs))
    if not targets:
        return measurement
    if len({t[0] for t in targets}) != 1:
        measurement['target_basis'] = 'conflicting_targets'
        return measurement
    target, basis, refs = targets[0]
    measurement.update(target=target, target_basis=basis, target_candidate_ids=refs,
        target_exceeded=inventory['identified'] > target)
    if target > 0:
        measurement.update(found_percent=100 * inventory['identified'] / target,
                           supported_percent=100 * inventory['supported'] / target)
    return measurement
