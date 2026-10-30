"""Make a proposed research plan respect the report specification accepted earlier.

The optional answer-design step defines sections, identifying fields and evidence
requirements before planning. bind_report applies those definitions to the matching
plan goals and replaces proposed completion criteria when the specification has
explicit checks. For example, a report asking for researchers must not silently
become a count of institutions. Supporting research goals must link to known answer
requirements when that policy is enabled. The function changes the proposed plan
in memory and can raise on incompatible references; it makes no model call or file
write, and a rejected plan may already be partially updated.
"""
from projectclarity.answer_schema.contracts import AnswerSchema


def bind_report(plan, schema, *, require_mappings=False):
    """Apply the accepted report specification to matching questions in a research plan.

    The answer-design model already specified the report sections, required fields and
    evidence requirements. This function makes the proposed plan use those definitions
    instead of replacing them with a different deliverable. Supporting goals must link
    to known answer requirements when require_mappings is enabled.

    Args:
        plan: Mutable proposed plan before acceptance or human-review persistence.
        schema: Saved answer specification, or None for legacy runs.
        require_mappings: Enforce explicit auxiliary mappings for new versioned runs;
            False preserves previously accepted legacy plans during review.
    Returns:
        None. Applies report questions, required fields and evidence requirements
        to matching goals. Raises ValueError if the planner omitted a report goal;
        new-policy research-only goals must explicitly map to known contract requirements.
        Report-goal mappings include their structural section requirements. Explicit contract checks
        replace planner-proposed completion criteria; old contracts retain their
        original criteria. No calls or file writes.
    """
    if not schema:
        return
    from projectclarity.research_schemas import ResultShape, CompletionCriterion
    report = AnswerSchema.model_validate(schema)
    goals = {g.goal_id: g for g in plan.goals}
    if any(s.goal_id not in goals for s in report.sections):
        raise ValueError('Plan omitted a required answer-schema goal')
    if report.completion_checks:
        known = {c.criterion_id for c in report.completion_checks}
        section_ids = {s.goal_id for s in report.sections}
        for goal in plan.goals:
            if set(goal.requirement_ids) - known:
                raise ValueError('Goal references an unknown answer requirement')
            structural = {c.criterion_id for c in report.completion_checks if goal.goal_id in c.goal_ids}
            goal.requirement_ids = sorted(set(goal.requirement_ids) | structural)
            if require_mappings and goal.goal_id not in section_ids and not goal.requirement_ids:
                raise ValueError('Auxiliary goal must declare the answer requirements it serves')
        plan.completion_criteria = [CompletionCriterion.model_validate(c.model_dump(
            exclude={'unit', 'result_goal_id', 'evidence_required'})) for c in report.completion_checks]
    elif any(not any(s.goal_id in c.goal_ids for c in plan.completion_criteria) for s in report.sections):
        raise ValueError('Every report section requires a completion check')
    for section in report.sections:
        goal = goals[section.goal_id]
        goal.question = section.question
        goal.evidence_required = section.evidence_requirements
        goal.result_shape = (ResultShape(unit=section.unit, required_fields=[f.name for f in section.fields])
                             if section.fields else None)
