"""Keep cited answers usable when fields, batches or bounded recovery fail.

The real HTTP workflow, receipts, reuse and validators run with synthetic provider
replies. These checks prove isolation and coverage disclosure, not semantic accuracy.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from projectclarity.reporting.answer_item_selection import validate_selection
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_batched_reporting import many_facts
from tests.integration.test_item_reporting import contribution
from tests.integration.test_research_graph import wait_for_completion


class ResilientReportTests(unittest.TestCase):
    """Exercise missing-field isolation and partial-report retry through production."""
    def test_nested_split_failure_never_skips_independent_siblings(self):
        """Fail one leaf twice and still assess every other split leaf through HTTP.

        Args: None; model replies/counts are fixtures, routing and validators real.
        Returns: None; proves complete dispatch coverage, bounded retry, partial
            accounting and preserved failed attempts without invented exclusions.
        """
        class Caller:
            """Force admission splitting and a repeatably malformed first leaf."""
            model='offline';provider='fixture';contract_id='split-failure-test'
            def __init__(self):
                """Args: None. Returns: None; initialize dispatched input log."""
                self.calls=[]
            def count_input_tokens(self,request):
                """Args: request: Full input. Returns: High count above two records."""
                return 17000 if len(json.loads(request['messages'][0]['content'])['facts'])>2 else 100
            def __call__(self,stage,request,sid):
                """Args: stage,request,sid: Production call. Returns: JSON or broken text."""
                facts=json.loads(request['messages'][0]['content'])['facts']
                ids=[f['id'] for f in facts];self.calls.append(ids)
                if 'F1' in ids:return '{"items":[]}'
                return json.dumps({'items':[contribution(f['id'],f['id'],f['fact']['statement']) for f in facts],
                                   'unused_fact_ids':[]})
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,6);state.pop('report_scheme',None)
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state);sid=state['session_id']
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code,202)
                    done=wait_for_completion(client,sid)
                    self.assertEqual(done['status'],'report_partial',done.get('report'))
                    self.assertEqual({fid for ids in caller.calls for fid in ids},{f'F{i}' for i in range(1,7)})
                    self.assertEqual(sum('F1' in ids for ids in caller.calls),2)
                    self.assertEqual(done['report']['facts_accounted'],5)
                    self.assertEqual(done['report']['unreviewed_facts'],1)
                    self.assertEqual(done['report']['model_calls'],len(caller.calls))
                    report=client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(report['exclusions'],[])
                    tasks=root/sid/done['report']['artifact_directory']/'tasks'
                    attempts=[v for p in tasks.glob('*.json') if len((v:=json.loads(p.read_text())).get('output_attempts',[]))==2]
                    self.assertEqual(len(attempts),1)
                    self.assertEqual(attempts[0]['status'],'failed')
            finally:sessions.close()

    def test_null_insufficient_values_are_withheld_not_promoted(self):
        """Treat explicit absence as unknown, never a supported cited answer.

        Args: None.
        Returns: None; audits absent values while rejecting supported null values.
        """
        packet={'requirements':[{'id':'R1'},{'id':'R2'}],'facts':[{'id':'F1'}]}
        item=contribution('Acme','F1','Acme')
        item['fields'].append({'requirement_id':'R2','value':None,'status':'insufficient','fact_ids':['F1']})
        result=validate_selection(json.dumps({'items':[item],'unused_fact_ids':[]}),packet,packet['facts'])
        self.assertNotIn('_issues',result)
        self.assertEqual(result['entries'][0]['fields'][1]['status'],'unknown')
        self.assertEqual(result['entries'][0]['fields'][1]['fact_ids'],[])
        self.assertIsNone(result['_recovery']['withheld_insufficient_fields'][0]['value'])
        item['fields'][1]['status']='supported'
        invalid=validate_selection(json.dumps({'items':[item],'unused_fact_ids':[]}),packet,packet['facts'])
        self.assertEqual(len(invalid['_issues']),1)
        self.assertEqual(invalid['entries'][0]['fields'][1]['status'],'unknown')

    def test_field_retry_retains_earlier_valid_values(self):
        """Correct malformed fields once without losing already cited contributions.

        Args: None; routes through actual HTTP reporting with fixture generation.
        Returns: None; confirms two calls, successful recovery and retained conflict.
        """
        class Caller:
            """Return one extra-property failure followed by a changed valid value."""
            model='offline'
            def __init__(self):
                """Args: None. Returns: None; initialize attempt count."""
                self.calls=0
            def count_input_tokens(self,request):
                """Args: request: Actual payload. Returns: Safe fixture input count."""
                return 100
            def __call__(self,stage,request,sid):
                """Args: stage,request,sid: Production call. Returns: Two controlled replies."""
                self.calls+=1
                item=contribution('Acme','F1','Original value' if self.calls==1 else 'Corrected value')
                if self.calls==1:
                    item['fields'].append({'requirement_id':'R2','value':'Toronto','status':'supported','fact_ids':['F1'],'location':'Toronto'})
                return json.dumps({'items':[item],'unused_fact_ids':['F2']})
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,2);state.pop('report_scheme',None)
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state);sid=state['session_id']
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report');done=wait_for_completion(client,sid)
                    self.assertEqual(done['status'],'report_completed',done.get('report'))
                    self.assertEqual(caller.calls,2)
                    report=client.get(f'/api/setup/{sid}/report-data').json()
                    field=report['entries'][0]['fields'][0]
                    self.assertIn('Original value',field['value'])
                    self.assertIn('Corrected value',field['value'])
                    self.assertEqual(field['status'],'conflicting')
                    self.assertEqual(done['report']['model_calls'],2)
            finally:sessions.close()

    def test_missing_invalid_and_uncited_fields_preserve_good_siblings(self):
        """Args: None. Returns: None; invalid values never gain invented citations."""
        packet={'requirements':[{'id':'R1'},{'id':'R2'}], 'facts':[{'id':'F1'},{'id':'F2'}]}
        item=contribution('Person','F1','Person')
        item['fields'] += [dict(requirement_id='R2',value='No date stated',status='insufficient',fact_ids=[]),
            dict(requirement_id='R2',value='Unsupported',status='supported',fact_ids=[]),
            dict(requirement_id='R2',value='Unknown reference',status='supported',fact_ids=['F999']),
            dict(requirement_id='R2',status='supported',fact_ids=['F2'])]
        result=validate_selection(json.dumps({'items':[item],'unused_fact_ids':[]}),packet,packet['facts'])
        self.assertEqual(result['entries'][0]['fields'][0]['value'],'Person')
        self.assertEqual(result['entries'][0]['fields'][1]['status'],'unknown')
        self.assertEqual(result['_recovery']['missing_fact_ids'],['F2'])
        self.assertEqual(len(result['_recovery']['withheld_insufficient_fields']),1)
        self.assertEqual(len(result['_issues']),3)

    def exercise(self, recovery=False, fields=False):
        """Publish useful partial output, then repair only unresolved requests.

        Args: recovery: Fail a targeted recovery after a usable parent response;
            fields: Return an uncited supported value instead of a batch failure.
        Returns: None; asserts visible partial data/CSV, unreviewed IDs, exact cached
            sibling reuse, completed retry and no upstream request types.
        """
        class Caller:
            """Inject a single batch fault; capture exact dispatched fact IDs."""
            model='offline'
            def __init__(self):
                """Args: None. Returns: None; initialize fault and request log."""
                self.fail=True;self.calls=[]
            def count_input_tokens(self, request):
                """Args: request: full request; Returns: small simulated input count."""
                return 100
            def __call__(self,stage,request,sid):
                """Args: stage,request,sid: actual call; Returns: reply or injected error."""
                facts=json.loads(request['messages'][0]['content'])['facts']
                ids=[row['id'] for row in facts];self.calls.append(ids)
                if self.fail and not fields and ('F1' in ids if not recovery else ids==['F2']):
                    raise RuntimeError('Offline batch failure')
                selected=facts[:-1] if self.fail and recovery and 'F1' in ids else facts
                items=[contribution(row['id'],row['id'],row['fact']['statement']) for row in selected]
                if self.fail and fields and 'F1' in ids:
                    items[0]['fields'][0]['fact_ids']=[]
                return json.dumps({'items':items, 'unused_fact_ids':[]})
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,6);state.pop('report_scheme',None)
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state);sid=state['session_id']
            try:
                with patch('projectclarity.reporting.batched_reporting.MAX_BATCH_FACTS',2),TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report');partial=wait_for_completion(client,sid)
                    self.assertEqual(partial['status'],'report_partial',partial.get('report'))
                    report=client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertGreater(len(report['entries']),0)
                    self.assertEqual(report['coverage']['unreviewed_report'],0 if fields else 1 if recovery else 2)
                    if not fields:self.assertIn('F2',report['coverage']['unreviewed_report_fact_ids'])
                    else:self.assertEqual(report['coverage']['withheld_invalid_fields'],1)
                    self.assertFalse(report['exclusions'])
                    self.assertIn('Partial report',client.get(f'/api/setup/{sid}/report.csv').text)
                    caller.fail=False;prior=len(caller.calls)
                    response=client.post(f'/api/setup/{sid}/report/retry',json={'previous_started_at':partial['report']['started_at']})
                    self.assertEqual(response.status_code,202)
                    complete=wait_for_completion(client,sid)
                    self.assertEqual(complete['status'],'report_completed',complete.get('report'))
                    self.assertEqual(complete['report']['facts_accounted'],6)
                    self.assertEqual(len(caller.calls)-prior,1)
                    self.assertGreaterEqual(complete['report']['reused_tasks'],2)
            finally:sessions.close()

    def test_one_failed_batch_does_not_destroy_report(self):
        """Args: None. Returns: None; sibling output remains visible and retryable."""
        self.exercise()

    def test_failed_recovery_preserves_parent_findings(self):
        """Args: None. Returns: None; useful parent output survives child failure."""
        self.exercise(recovery=True)

    def test_invalid_field_can_be_retried_without_repeating_valid_batches(self):
        """Args: None. Returns: None; quarantined field remains unknown, not supported."""
        self.exercise(fields=True)
