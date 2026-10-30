"""Track evidence requirements proposed by the optional preliminary source study.

The study can describe how to assess a question, such as comparing alternatives
or assembling a timeline. These helpers validate its goal references, assign IDs
to proposed checks and display their later assessment statuses. Establishing an
assessment method does not mean that its evidence has already been collected.

Older plans may use the study checks in addition to their original criteria when
early stopping is enabled. Runs with explicit designed-answer requirements keep
the study suggestions advisory instead. Numerical estimates do not become exact
targets, and running a study does not itself enable automatic stopping.
"""
from projectclarity.research_schemas import CompletionCriterion


def retain_readiness_profile(session, profile):
    """Save the preliminary study's proposed evidence checks after validating goal links.

    The profile describes how the question could be assessed, not whether it has
    already been answered. Assign check IDs that do not collide with plan criteria
    and retain rejected goal references separately for inspection.

    Args:
        session: Mutable run containing the original plan and early-study state.
        profile: Parsed task-specific profile returned in a study phase.
    Returns:
        Rejection receipts for invalid check goals. Persist valid requirements and
        unresolved questions as preliminary context, never facts or satisfied checks.
        IDs are assigned before recursive assessment; refinement replaces the draft.
    """
    goals = {g.goal_id for g in session.plan.goals}
    used = {c.criterion_id for c in session.plan.completion_criteria}
    retained, rejected = [], []
    for index, check in enumerate(profile.coverage_checks, 1):
        if set(check.goal_ids) - goals:
            rejected.append({'check_index': index, 'error_code': 'unknown_goal'})
            continue
        identifier = f'CS{index}'
        while identifier in used:
            identifier = 'S_' + identifier
        used.add(identifier)
        retained.append({'check_id': identifier, **check.model_dump(mode='json')})
    session.coverage_study['readiness'] = {**profile.model_dump(mode='json'),
        'coverage_checks': retained, 'basis': 'model_proposed_requirements',
        'automatic_stopping': False}
    session.coverage_study['rejected_checks'] = rejected
    return rejected


def checked_coverage_outcome(session, proposed):
    """Check whether the preliminary study described a method for every research goal.

    The label established means a proposed coverage method exists, not that the
    research is complete. Missing checks, uncovered goals or unresolved questions
    can downgrade that label to partial or unknown.

    Args:
        session: Run whose stored readiness profile has validated goal references.
        proposed: Model's study outcome, or unnecessary for an explicit skip decision.
    Returns:
        Outcome string. Established/unnecessary is downgraded when required checks
        are missing, goals are uncovered or ambiguities remain. This only validates
        structural coverage; it does not certify the method's semantic adequacy.
    """
    study = session.coverage_study
    profile = study['readiness']
    checks = profile['coverage_checks']
    if proposed in {'established', 'unnecessary'}:
        # A skipped separate study may rely entirely on the original plan's criteria.
        covered = {g for c in checks for g in c['goal_ids']}
        if proposed == 'unnecessary' and not checks:
            covered = {g for c in session.plan.completion_criteria for g in c.goal_ids}
        if not covered:
            return 'unknown'
        if (study.get('rejected_checks') or profile['unresolved_questions']
                or {g.goal_id for g in session.plan.goals} - covered):
            return 'partial'
    return proposed


def readiness_criteria(session):
    """Convert preliminary-study evidence requirements into supplemental assessment checks.

    For older plans, those checks can supplement the original completion criteria.
    Runs with explicit designed-answer checks keep study suggestions advisory and
    return no extra mandatory criteria here. Estimates never become exact targets.

    Args:
        session: Run containing a new or legacy study; the original plan is never edited.
    Returns:
        For explicit answer contracts, an empty list: study suggestions remain
        advisory, never additional mandatory checks. Otherwise supplemental
        CompletionCriterion objects, including initial open questions,
        or an empty list for legacy/running
        studies. Numerical study estimates never become targets. Enumeration checks
        require a separately evidenced denominator; other checks need no population count.
        Evidence requirements remain separate in the profile and assessment payload so
        combining valid fields cannot overflow the criterion description's length limit.
    """
    if (session.answer_schema or {}).get('completion_checks'):
        return []
    study = session.coverage_study
    if not study or study.get('status') == 'running':
        return []
    criteria = [CompletionCriterion(criterion_id=c['check_id'], goal_ids=c['goal_ids'],
        description=c['description'],
        kind=c['kind'], measurement_method=c['assessment_method'],
        target_basis='research' if c['kind'] == 'enumeration' else 'not_applicable',
        target_count=None, limitations=[])
        for c in study.get('readiness', {}).get('coverage_checks', [])]
    used = {c.criterion_id for c in criteria + list(session.plan.completion_criteria)}
    for index, question in enumerate(study.get('readiness', {}).get('unresolved_questions', []), 1):
        identifier = f'CQ{index}'
        while identifier in used:
            identifier = 'Q_' + identifier
        used.add(identifier)
        criteria.append(CompletionCriterion(criterion_id=identifier,
            goal_ids=[g.goal_id for g in session.plan.goals], description=question,
            kind='evidence_sufficiency', measurement_method=question,
            target_basis='not_applicable', target_count=None, limitations=[]))
    return criteria


def readiness_lines(study, coverage):
    """Format the preliminary study's assessment method and latest check statuses for display.

    Show what evidence the study proposed collecting separately from whether later
    research satisfied those requirements. The displayed counts are numbers of
    checks, not the fraction of a real-world population discovered.

    Args:
        study: Saved early-study dictionary, including legacy versions without a profile.
        coverage: Saved main-loop assessments; an empty dictionary means not yet assessed.
    Returns:
        Terminal lines describing the method and each check's latest model-assessed
        status. Counts are checks, not population coverage or a completion percentage;
        stopping still requires the separate explicit operator setting.
        Legacy records retain their original reporting and produce no extra lines.
    """
    profile = study.get('readiness')
    if not profile:
        return []
    checks = profile['coverage_checks']
    progress = {c['criterion_id']: c for c in coverage.get('readiness_progress', [])}
    lines = [f"Coverage method: {study.get('coverage_outcome', 'unknown')} · {profile['answer_type']}",
        'Numerical estimate: ' + ('applicable; uncertainty retained' if profile['numeric_estimate_applicable']
            else 'not applicable to this task (not a failure)'),
        f"Answer-readiness checks: {len(checks)}; latest model assessment; stopping remains opt-in."]
    for check in checks:
        row = progress.get(check['check_id'], {})
        lines.append(f"  {check['check_id']} [{row.get('status', 'not_assessed')}]: {check['description'][:200]}")
    if coverage.get('history') and coverage['history'][-1].get('readiness_error'):
        lines.append('Latest readiness assessment rejected: ' + coverage['history'][-1]['readiness_error'])
    return lines
