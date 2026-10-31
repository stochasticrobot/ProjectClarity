"""Preflight authored coverage-only fixtures before any paid comparison.

This checks the intentionally narrow, single-source-per-qualification fixture
format against existing application contracts and reference ownership. It does
not judge truth, infer a missing target, validate arbitrary production evidence,
or send requests. Expected answers must remain outside the model input.
"""
from projectclarity.answer_schema.contracts import AnswerCompletionCheck
from projectclarity.research_schemas import Goal
from projectclarity.coverage.payload_normalization import expand_qualification_payload


def validate_case(payload):
    """Reject malformed small evaluation cases before freezing or invoking a model.

    Args:
        payload: Inline or normalized synthetic coverage JSON with full candidates,
            source IDs, goals, criteria and per-entry qualification assessments.
    Returns:
        Expanded payload after type, reference and source-ownership checks.
        Raises ValueError for unsupported contracts or inconsistent attribution.
        This single-source fixture rule is not imposed on production evidence.
    """
    expanded = expand_qualification_payload(payload)
    goals = {row['goal_id']: Goal.model_validate(row) for row in expanded['goals']}
    criteria = [AnswerCompletionCheck.model_validate(row) for row in expanded['criteria']]
    if len(goals) != len(expanded['goals']) or len({c.criterion_id for c in criteria}) != len(criteria):
        raise ValueError('duplicate_fixture_identifier')
    for criterion in criteria:
        if set(criterion.goal_ids) - goals.keys():
            raise ValueError('unknown_fixture_goal')
        if criterion.result_goal_id is not None:
            if criterion.result_goal_id not in goals:
                raise ValueError('unknown_fixture_goal')
            goal = goals[criterion.result_goal_id]
            if goal.result_shape is None or criterion.unit != goal.result_shape.unit:
                raise ValueError('fixture_population_unit_mismatch')
    candidates = {row['candidate_id']: row for row in expanded['candidates']}
    if len(candidates) != len(expanded['candidates']):
        raise ValueError('duplicate_fixture_candidate')
    sources = expanded['sources']
    for candidate in candidates.values():
        if candidate['source_id'] not in sources:
            raise ValueError('unknown_fixture_source')
    for gid, ledger in expanded['result_inventory'].items():
        if gid not in goals or ledger['unit'] != goals[gid].result_shape.unit:
            raise ValueError('fixture_population_unit_mismatch')
        for row in ledger['items']:
            if set(row['candidate_ids']) - candidates.keys():
                raise ValueError('unknown_fixture_candidate')
            for requirement in row['qualification_requirements']:
                for assessment in requirement['assessments']:
                    candidate = candidates.get(assessment['candidate_id'])
                    if candidate is None:
                        raise ValueError('unknown_fixture_candidate')
                    if not (candidate['source_id'] == assessment['source_id']
                            == assessment['context']['source_id']):
                        raise ValueError('fixture_source_attribution_mismatch')
    return expanded
