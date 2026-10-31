"""Run a dashboard-launched research worker with simulated provider responses.

The tests start this helper in a child process with ordinary research arguments.
It installs a fake provider HTTP responder, denies external networking and then
enters projectclarity.__main__. The real workflow, parsing and file persistence
remain in use. Replies are handwritten fixture data, not findings from a live model;
users launch the normal dashboard rather than this helper.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import json
import runpy
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx2

from tests.extraction.extraction_test_fixtures import proposed_batch
from tests.planning.planning_fixtures import planning_reply


def reply(request):
    """Answer actual SDK requests without a network connection or real credentials.

    Args:
        request: Serialized provider request from the unmodified production graph.
    Returns:
        Schema-valid synthetic answer design, plan, extraction, review or coverage with known usage.
        Delays briefly so tests can inspect running state; unexpected tasks fail.
    Review replies include authored report-mapping judgments; these do not
    establish live semantic recognition accuracy.
    """
    time.sleep(0.4)
    body = json.loads(request.content)
    title = body['output_config']['format']['schema']['title']
    if title == 'AnswerSchema':
        from tests.research.test_answer_schema import specification
        answer = specification(('name', 'institution'), ('person_name', 'organization_name'))
        answer['completion_checks'] = [dict(criterion_id='C1', goal_ids=['G1'],
            description='Support the requested source findings.', kind='evidence_sufficiency',
            measurement_method='Inspect source support and report fields.', target_basis='not_applicable',
            target_count=None, limitations=[], unit='requested findings', result_goal_id=None,
            evidence_required=['Support each requested field with source evidence.'])]
    elif title == 'ResearchPlan':
        plan = json.loads((Path(__file__).parents[1] / 'planning/fixtures/valid_research_plan.json').read_text())
        plan['clarifications'] = ['What should be included?'] if 'clarify' in ' '.join(sys.argv) else []
        if 'Target inventory' in ' '.join(sys.argv):
            plan['goals'][0]['result_shape'] = {'unit': 'one researcher',
                'required_fields': ['name', 'institution']}
        if '--design-answer' in sys.argv:
            plan['goals'][1]['requirement_ids'] = ['C1']
        answer = planning_reply(plan)
    elif title == 'ExtractionBatch':
        answer = {'entities': [], 'claims': []} if 'empty' in ' '.join(sys.argv) else proposed_batch()
        if 'Target inventory' in ' '.join(sys.argv):
            answer['claims'][0].update(claim_kind='relational', research_roles=['answer_evidence'],
                result_records=[{'goal_id': 'G1', 'fields': [
                    {'name': 'name', 'value': 'Alice'}, {'name': 'institution', 'value': 'Lab X'}]}])
    elif title == 'ClaimReviewBatch':
        payload = json.loads(body['messages'][0]['content'])
        answer = {'assessments': [dict(candidate_id=c['candidate_id'], verdict='supported',
            in_scope=True, rationale='Synthetic test: the saved quotation supports this statement.',
            report_mappings=fixture_mappings(c, payload), eligibility_assessments=fixture_eligibility(c, payload), evidence_quotes=['Alice works at Lab X.']) for c in payload['candidates']]}
    elif title == 'CoverageAssessment':
        payload = json.loads(body['messages'][0]['content'])
        answer = dict(assessments=[dict(criterion_id=c['criterion_id'], judgment='satisfied',
            target_count=None, result_goal_id=None, items=[], benchmark_candidate_ids=[],
            evidence_candidate_ids=[c['candidate_id'] for c in payload['candidates']],
            gaps=[], rationale='Controlled fixture: all requested source fields are supported.')
            for c in payload['criteria']], next_searches=[], scope_estimates=[])
    else:
        raise AssertionError(title)
    return httpx2.Response(200, json=dict(id='msg_dashboard_fixture', type='message', role='assistant',
        model='claude-haiku-5-5', content=[dict(type='text', text=json.dumps(answer))],
        stop_reason='end_turn', stop_sequence=None, usage=dict(input_tokens=150, output_tokens=220)))


def main():
    """Run passed-through production arguments with network-forbidden provider transport.

    Args:
        None; sys.argv contains the exact production arguments from the dashboard.
    Returns:
        Never normally returns: production __main__ exits with its actual result.
        Only provider transport, credentials and accidental network access are patched.
    """
    with (httpx2.Client(transport=httpx2.MockTransport(reply)) as client,
          patch('langchain_anthropic.chat_models._get_default_httpx_client', return_value=client),
          patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'SYNTHETIC_DASHBOARD_TEST_KEY'}),
          patch('socket.socket.connect', side_effect=AssertionError('Unexpected live network'))):
        runpy.run_module('projectclarity', run_name='__main__')


if __name__ == '__main__':
    main()
