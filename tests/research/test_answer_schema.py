"""Check report specifications, identity grouping and their use in research.

Direct tests construct report definitions and observations to examine name formatting,
conflicts and missing identity. Integration cases run design, planning, extraction
and reporting through the command-line workflow with simulated service responses.
They exercise several answer forms without claiming live model design quality.
"""
import copy
import json
import unittest

from projectclarity.answer_schema.contracts import AnswerSchema
from projectclarity.answer_schema.binding import bind_report
from projectclarity.research_results import result_progress
from tests.research import test_result_reporting_cli as reporting
from tests.research import test_result_accounting as accounting


def specification(fields=('name', 'riding'), kinds=('person_name', 'text'), identity=None, format='records'):
    """Create a test-only requested report, never a reference answer.

    Args:
        fields: Required names, empty for narrative sections.
        kinds: Corresponding semantic field categories.
        identity: Optional explicit identifying subset; defaults to all fields.
        format: Requested presentation kind.
    Returns:
        JSON-compatible contract accepted by the production AnswerSchema model.
    """
    return dict(title='Requested research report', scope='Only the requested source items.', as_of=None,
        sections=[dict(goal_id='G1', title='Results', question='List the requested items.',
            format=format, unit='source items', fields=[dict(name=f, meaning='Requested '+f,
                value_kind=k, multiplicity='single', absence_meaning='Unknown unless evidence explicitly establishes absence.') for f,k in zip(fields,kinds)],
            identity_fields=list(fields if identity is None else identity), evidence_requirements=['Support every requested field with source evidence.'])],
        interpretation_notes=[])


class AnswerSchemaTests(unittest.TestCase):
    """Verify identity/attribute separation, provenance and graph-level delivery."""

    def inventory(self, observations, *, identity=('name', 'riding')):
        """Build anchored observations for field-merging regression cases.

        Args:
            self: Test owner with no persistent research data.
            observations: Pairs of field dictionaries and candidate review statuses.
            identity: Explicit fields determining one report entry.
        Returns:
            Inventory produced by the real shared accounting implementation.
        """
        f=accounting.ResultAccountingTests();f.setUp();self.addCleanup(f.doCleanups)
        schema=specification(('name','riding','affiliation'),('person_name','text','text'),identity)
        goal=copy.deepcopy(f.goal);goal['result_shape']=dict(unit='source items',required_fields=['name','riding','affiliation'])
        candidates=[]
        for i,(values,status) in enumerate(observations):
            c=copy.deepcopy(f.candidate);c['candidate_id']=f'C{i}';c['adjudication_status']=status
            c['proposal']['result_records'][0]['fields']=[dict(name=k,value=v) for k,v in values.items()]
            candidates.append(c)
        data=dict(answer_schema=schema,plan=dict(goals=[goal]),extraction=dict(candidates=candidates))
        return result_progress(data)['goals'][0]['results']

    def test_typed_identity_does_not_depend_on_occupation_labels(self):
        """Join name-order variants while retaining identity across attribute upgrades.

        Args:
            self: Test owner using no free-text entity labels at all.
        Returns:
            None; asserts stable ID, separate per-field support and one complete entry.
        """
        first=dict(name='Rowan Example',riding='North')
        before=self.inventory([(first,'supported')])
        after=self.inventory([(first,'supported'),(dict(name='Example, Rowan',riding='North',affiliation='Independent'),'supported')])
        self.assertEqual((before['identified'],before['incomplete'],before['supported']),(1,1,0))
        self.assertEqual((after['identified'],after['supported']),(1,1))
        self.assertEqual(before['records'][0]['result_id'],after['records'][0]['result_id'])
        self.assertEqual(after['records'][0]['field_status']['affiliation']['status'],'supported')

    def test_conflicts_and_missing_identities_never_manufacture_completion(self):
        """Keep homonyms and contradictory attributes visible without blind joins.

        Args:
            self: Test owner using a person-plus-location identity.
        Returns:
            None; differing affiliations leave one unresolved entry and absent identity
            cannot be joined to a partial record or counted as a discovered entry.
        """
        rows=[(dict(name='Rowan Example',riding='North',affiliation=v),'supported') for v in ['A','B']]
        r=self.inventory(rows)
        self.assertEqual((r['identified'],r['pending'],r['supported']),(1,1,0))
        r=self.inventory([(dict(name='Rowan Example'), 'supported'),(dict(riding='North',affiliation='A'),'supported')])
        self.assertEqual((r['identified'],r['supported']),(0,0))
        self.assertEqual(r['unassigned_count'],2)
        self.assertEqual(r['records'], [])
        r=self.inventory([(dict(name='Rowan Example',riding='North',affiliation='A'),'supported'),
                          (dict(name='Example, Rowan',riding='North',affiliation='B'),'pending')])
        self.assertEqual((r['identified'],r['pending'],r['supported']),(1,1,0))

    def test_schema_rejects_ambiguous_contracts(self):
        """Reject missing/unknown identity fields and provenance-as-identity.

        Args:
            self: Test owner validating provider-output boundaries.
        Returns:
            None; malformed contracts fail before any research planning.
        """
        for fields,identity in [(('name',),[]),(('name',),['other']),(('source_url',),['source_url'])]:
            with self.subTest(fields=fields,identity=identity),self.assertRaises(ValueError):
                AnswerSchema.model_validate(specification(fields,('text',),identity))
        self.assertEqual(AnswerSchema.model_validate(specification((),(),format='narrative')).sections[0].fields,[])
        from tests.research.research_test_fixtures import example_session
        plan=example_session().plan
        plan.completion_criteria=[]
        with self.assertRaises(ValueError):
            bind_report(plan,specification())

    def test_production_report_design_for_diverse_answer_shapes(self):
        """Exercise real design/planning/extraction/reporting for five task families.

        Args:
            self: Test owner replacing only external HTTP/model transports.
        Returns:
            None; proves schema reaches planner/extractor, controls final output,
            and consumes the ordinary budget. Does not prove live-model quality.
        """
        cases=[(('name','organization'),('person_name','organization_name'),'records'),
               (('event','date'),('text','date'),'timeline'),
               (('funder','recipient'),('organization_name','organization_name'),'records'),
               (('ancestor','descendant'),('person_name','person_name'),'records'),
               (('event','date'),('text','date'),'narrative')]
        for fields,kinds,format in cases:
            with self.subTest(fields=fields):
                f=reporting.ResultReportingCLITests();f.setUp();self.addCleanup(f.doCleanups)
                f.fields=list(fields);f.partial=False;f.relational=False
                schema=specification((),(),format=format) if format=='narrative' else specification(fields,kinds,format=format)
                f.cli.plan['completion_criteria']=[dict(criterion_id='K1',goal_ids=['G1'],
                    description='Establish the requested source items.',
                    kind='evidence_sufficiency' if format=='narrative' else 'enumeration',
                    measurement_method='Inspect the provided evidence and required fields.',
                    target_basis='not_applicable' if format=='narrative' else 'user_request',
                    target_count=None if format=='narrative' else 2,limitations=[])]
                schema['completion_checks'] = [{**f.cli.plan['completion_criteria'][0],
                    'unit': schema['sections'][0]['unit'],
                    'result_goal_id': None if format=='narrative' else 'G1',
                    'evidence_required': ['Support the requested report with source evidence.']}]
                f.cli.plan['goals'][1]['requirement_ids'] = ['K1']
                original=f.provider;run_cli=f.cli.run_cli;seen=[]

                def provider(request):
                    """Respond at the SDK boundary and inspect the actual input contract.

                    Args:
                        request: Real serialized SDK request, never a node stub.
                    Returns:
                        Synthetic schema or source-derived fixture reply.
                    """
                    wire=json.loads(request.content);title=wire['output_config']['format']['schema']['title']
                    seen.append(title)
                    payload=json.loads(wire['messages'][0]['content'])
                    if title=='AnswerSchema':
                        self.assertIn('original_question',payload)
                        return f.cli.response(text=json.dumps(schema))
                    if title=='CoverageAssessment':
                        self.assertEqual(payload['answer_schema'],schema)
                        return f.cli.response(text=json.dumps(dict(assessments=[dict(
                            criterion_id='K1',judgment='satisfied',items=[],
                            result_goal_id=None if format=='narrative' else 'G1',
                            target_count=None if format=='narrative' else 2,benchmark_candidate_ids=[],
                            evidence_candidate_ids=[c['candidate_id'] for c in payload['candidates']],
                            gaps=[],rationale='Fixture evidence satisfies the requested report.')],next_searches=[])))
                    if title in {'ResearchPlan','ExtractionBatch'}:
                        self.assertEqual(payload['answer_schema'],schema)
                    response=original(request)
                    if format=='narrative' and title=='ExtractionBatch':
                        reply=json.loads(response.json()['content'][0]['text'])
                        for claim in reply['claims']:
                            claim['result_records']=[]
                        return f.cli.response(text=json.dumps(reply))
                    return response

                def run(provider,*args,**kwargs):
                    """Enable report design at the public terminal entry point.

                    Args:
                        provider: Synthetic network handler.
                        args: Existing research flags.
                        kwargs: Existing fixture transport settings.
                    Returns:
                        Normal command result with no node substitution.
                    """
                    return run_cli(provider,*args,'--design-answer','--assess-coverage','--stop-when-satisfied',**kwargs)

                f.provider=provider;f.cli.run_cli=run
                code,out,err,directory,saved,answer=f.run_case(budget=12)
                self.assertEqual(code,0,err)
                self.assertEqual(seen[:2],['AnswerSchema','ResearchPlan'])
                self.assertEqual(saved['visited_nodes'][:3],['initialize_research','design_answer','plan_research'])
                self.assertEqual(answer['answer_schema'],schema)
                self.assertEqual(saved['workflow']['stop_reason'],'criteria_satisfied')
                if format=='narrative':
                    self.assertIsNone(answer['progress']['goals'][0]['results'])
                    self.assertIn('Section completeness requires its evidence checks',out)
                else:
                    self.assertEqual(answer['progress']['goals'][0]['results']['supported'],2)
                    self.assertIn('| '+fields[0]+' | '+fields[1],out)
                self.assertTrue((directory/'answer_schema.json').is_file())
                from projectclarity.dashboard.projection import snapshot
                view=snapshot(f.cli.output,directory.name)
                self.assertEqual(view['answer_schema'],schema)
                if format!='narrative':
                    self.assertEqual(view['targets'][0]['supported'],2)

    def test_invalid_design_stops_before_planning(self):
        """Reject repeated invalid designs after the single bounded correction.

        Args:
            self: Test owner intercepting the real provider transport.
        Returns:
            None; both charged attempts remain inspectable and no plan is accepted.
        """
        f=reporting.ResultReportingCLITests();f.setUp();self.addCleanup(f.doCleanups)
        code,_,err=f.cli.run_cli(lambda request:f.cli.response(text='{}'), '--design-answer',
            '--max-requests','3',plan_only=True,details=False)
        saved=json.loads(next(f.cli.output.glob('*/session.json')).read_text())
        self.assertNotEqual(code,0,err)
        self.assertEqual(saved['usage']['requests_attempted'],2)
        self.assertNotIn('plan_research',saved['visited_nodes'])
        self.assertIsNone(saved['answer_schema'])
