"""Build handwritten claim-to-report decisions for offline reviewer responses.

Older test responses predate the report-mapping fields now required by review.
This helper supplies explicit fixture decisions so those tests can still exercise
their intended paths. It is not a model reviewer and cannot demonstrate that a
live model recognizes missing report contributions.
"""


def fixture_mappings(candidate, payload):
    """Supply the new required contract for existing synthetic reviewer responses.

    Args:
        candidate: Fixture proposal; these tests do not evaluate semantic recognition.
        payload: Actual model input containing goal shapes.
    Returns:
        Explicit fixture judgments: supplied record fields are mapped; claims with
        no supplied fields are treated as context by these older fixtures. Dedicated
        mapping regressions override this with omitted/uncertain/invalid judgments.
        This helper must never be imported by application code.
    """
    proposal = candidate['proposal']
    rows = []
    for goal in payload.get('goals', []):
        if goal['goal_id'] not in proposal['goal_ids'] or not goal.get('result_shape'):
            continue
        fields = sorted({f['name'] for r in proposal.get('result_records', [])
                         if r['goal_id'] == goal['goal_id'] for f in r['fields']})
        if 'answer_evidence' not in proposal.get('research_roles', []):
            fields = []
        rows.append(dict(goal_id=goal['goal_id'], status='mapped' if fields else 'not_applicable',
            field_names=fields, rationale='Authored offline fixture mapping, not a live semantic judgment.'))
    return rows


def fixture_eligibility(candidate, payload):
    """Supply explicit positive inclusion judgments for existing transport fixtures.

    Args:
        candidate: Handwritten fixture claim whose answer records are intended to qualify.
        payload: Actual review request containing the application-generated requirements.
    Returns:
        One authored met judgment per answer-record requirement, with an exact fixture
        quotation. Dedicated qualification tests replace these judgments with unknown,
        incompatible or malformed decisions. This is not a semantic reviewer and must
        never be imported by production code or used to claim live model accuracy.
    """
    if 'answer_evidence' not in candidate['proposal'].get('research_roles', []):
        return []
    return [dict(record_index=index, goal_id=record['goal_id'], requirement_id=requirement['requirement_id'],
        verdict='met', rationale='Authored fixture inclusion judgment, not live semantic verification.',
        evidence_quotes=[candidate['proposal']['evidence'][0]['quote']], context_keys=[])
        for index, record in enumerate(candidate['proposal'].get('result_records', []))
        for requirement in payload.get('report_requirements', {}).get(record['goal_id'], [])]
