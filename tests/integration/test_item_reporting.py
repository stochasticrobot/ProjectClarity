"""Answer-item tests: code merge invariants and real HTTP report-only execution."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
from projectclarity.reporting.answer_item_selection import validate_selection,selection_request
from projectclarity.reporting.answer_item_assembly import assemble_items
from projectclarity.reporting.report_generation import validate_report
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_batched_reporting import many_facts
from tests.integration.test_research_graph import wait_for_completion


def contribution(label, fid, value, field='R1', detail='', order='', disposition='candidate'):
    """Build a small model-shaped answer contribution for deterministic checks.

    Args: label: Item name; fid: Cited fact; value: Field text; field: Requested ID;
        detail: Homonym qualifier; order: Requested order key; disposition: Gate.
    Returns: Synthetic item; no model or production parser is replaced.
    """
    return {'label':label,'fact_ids':[fid],'fields':[{'requirement_id':field,'status':'supported','value':value,'fact_ids':[fid]}],
            'distinguishing_detail':detail,'order_key':order,'disposition':disposition}


class ItemReportingTests(unittest.TestCase):
    """Separate useful output requirements from structural schema compliance."""

    def check_items(self,items,unused=(),count=4):
        """Validate synthetic contributions and assemble them through production code.

        Args: items: Model-shaped contributions; unused: Explicitly ignored IDs;
            count: Total offered facts.
        Returns: Assembled report and checked fragments for assertions.
        """
        packet={'requirements':[{'id':'R1'},{'id':'R2'}],'facts':[{'id':f'F{i+1}'} for i in range(count)]}
        fragment=validate_selection(json.dumps({'items':items,'unused_fact_ids':list(unused)}),packet,packet['facts'])
        report=assemble_items([fragment],packet)
        return report,fragment

    def test_complementary_fields_duplicates_partial_and_irrelevant(self):
        """Combine duplicate names and complementary fields, retaining partials.

        Args: None.
        Returns: None; checks useful list content rather than parse success alone.
        """
        report,fragment=self.check_items([contribution('Acme','F1','Acme'),contribution(' acme ','F2','Toronto','R2'),
            contribution('Acme','F3','Acme')],['F4'])
        self.assertEqual(len(report['entries']),1)
        self.assertEqual([f['value'] for f in report['entries'][0]['fields']],['Acme','Toronto'])
        self.assertEqual(report['entries'][0]['fields'][0]['fact_ids'],['F1','F3'])
        self.assertEqual(report['summary'],[])
        self.assertEqual(report['exclusions'][0]['fact_id'],'F4')
        self.assertEqual(fragment['_recovery']['missing_fact_ids'],[])
        partial,_=self.check_items([contribution('Partial','F1','Partial')],['F2','F3','F4'])
        self.assertEqual(partial['entries'][0]['fields'][1]['status'],'unknown')

    def test_conflicts_and_same_name_different_people(self):
        """Retain opposing amounts and distinguish homonyms using explicit tags.

        Args: None.
        Returns: None; no averaging, fuzzy name merge or silent value selection.
        """
        report,_=self.check_items([contribution('Project','F1','$10','R2'),contribution('Project','F2','$20','R2')],['F3','F4'])
        field=report['entries'][0]['fields'][1]
        self.assertEqual(field['status'],'conflicting')
        self.assertEqual(field['value'],'$10 | $20')
        people,_=self.check_items([contribution('Alex Lee','F1','Physicist',detail='Toronto'),
            contribution('Alex Lee','F2','Poet',detail='Ottawa')],['F3','F4'])
        self.assertEqual(len(people['entries']),2)

    def test_events_quotes_products_dates_and_amounts_are_literal(self):
        """Keep chronological keys and literal values without narrative synthesis.

        Args: None.
        Returns: None; event order and exact quotations survive assembly.
        """
        items=[contribution('Launch','F1','2024-03-02',order='2024-03-02'),
               contribution('Discovery','F2','“A quoted phrase.”',order='2023-01-01'),
               contribution('Product Q','F3','$19.99')]
        report,_=self.check_items(items,['F4'])
        self.assertEqual([e['label'] for e in report['entries']],['Discovery','Launch','Product Q'])
        self.assertEqual(report['entries'][0]['fields'][0]['value'],'“A quoted phrase.”')

    def test_excluded_subject_not_in_answer_and_missing_not_silently_dropped(self):
        """Keep a ruled-out organisation out of the answer while accounting for facts.

        Args: None.
        Returns: None; missing and unknown IDs remain explicit errors/recovery needs.
        """
        report,fragment=self.check_items([contribution('Institute','F1','Not a company',disposition='excluded')],['F2','F3'])
        self.assertEqual(report['entries'],[])
        self.assertEqual(fragment['_recovery']['missing_fact_ids'],['F4'])
        with self.assertRaises(ValueError):self.check_items([contribution('Bad','F999','Invented')])

    def test_http_filter_batches_to_list_no_model_merges(self):
        """Exercise V3 through HTTP with split batches and a deterministic transport.

        Args: None.
        Returns: None; one merged answer, no narrative calls, unchanged upstream
            artifacts and input evidence, stable fact references and CSV export.
        """
        class Caller:
            """Return useful item contributions for the actual filtering schema."""
            model='offline-items'
            def __init__(self):
                """Initialize recorded requests.

                Args: None.
                Returns: None; no external access.
                """
                self.calls=[]
            def count_input_tokens(self,request):
                """Provide an offline preflight count for fixture requests.

                Args: request: Actual complete provider input.
                Returns: Fixed safe count; does not claim real token measurement.
                """
                return 1000
            def __call__(self,stage,request,sid):
                """Select each offered fact into the same answer item.

                Args: stage: Actual label; request: Real payload; sid: Session ID.
                Returns: Item-filter JSON, never a narrative or merged-model response.
                """
                self.calls.append((stage,request,sid));data=json.loads(request['messages'][0]['content'])
                return json.dumps({'items':[contribution('Fixture company',f['id'],'Fixture company',data['requirements'][0]['id']) for f in data['facts']],'unused_fact_ids':[]})
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,8);state.pop('report_scheme');sid=state['session_id']
            originals={p:p.read_bytes() for p in (root/sid).rglob('*') if p.is_file()}
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with patch('projectclarity.reporting.batched_reporting.MAX_BATCH_FACTS',2),TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report');done=wait_for_completion(client,sid)
                    self.assertEqual(done['status'],'report_completed',done.get('report'))
                    report=client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(report['presentation'],'items')
                    self.assertEqual(len(report['entries']),1)
                    self.assertEqual(len(caller.calls),4)
                    self.assertTrue(all('-facts-' in stage for stage,_,_ in caller.calls))
                    self.assertEqual(report['summary'],[])
                    self.assertEqual(done['report']['facts_accounted'],8)
                    self.assertEqual(done['report']['merge_tasks_finished'],0)
                    self.assertEqual(client.get(f'/api/setup/{sid}/report.csv').status_code,200)
                    for p,raw in originals.items():self.assertEqual(p.read_bytes(),raw)
            finally:sessions.close()

    def test_repeated_field_contributions_are_combined_not_rejected(self):
        """Reproduce the two live responses containing repeated requirement fields.

        Args: None.
        Returns: None; distinct values/citations survive, exact duplicates collapse.
        """
        item=contribution('Product','F1','First capability','R2')
        item['fields'].append(contribution('Product','F2','Second capability','R2')['fields'][0])
        report,fragment=self.check_items([item],['F3','F4'])
        field=report['entries'][0]['fields'][1]
        self.assertEqual(field['value'],'First capability | Second capability')
        self.assertEqual(field['status'],'supported')
        self.assertEqual(field['fact_ids'],['F1','F2'])
        self.assertEqual(fragment['_recovery']['missing_fact_ids'],[])
