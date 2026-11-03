"""Check that reports keep the requested answer fields and cite the supplied facts.

The tests provide saved research information and invented report replies, including
invalid ones. They exercise report generation and its web actions, checking field
coverage, source references, excluded facts, saved results and spreadsheet exports.
The tests can catch broken report rules, but cannot prove the quality of real AI writing.
"""
import copy
import csv
from io import StringIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from fastapi.testclient import TestClient
from projectclarity.claim_review.claim_investigation import InvestigationRun
from projectclarity.reporting.report_generation import ReportRun, validate_report, report_csv
from tests.integration.test_claim_investigation import investigation_fixture, InvestigationCaller
from projectclarity.ux.web_app import Sessions, create_app


def report_fixture(root):
    """Prepare actual report knowledge through selection, triage and investigation.

    Args: root: Temporary session root.
    Returns: Completed investigation snapshot with one baseline and one accepted
        fact, explicitly selecting the historical V2 report contract. V3 tests
        remove that fixture-only setting before exercising the current path.
    """
    state=investigation_fixture(root)
    state['report_scheme']='legacy'
    InvestigationRun(root/state['session_id'],state,lambda _:None,InvestigationCaller(),Event()).execute()
    return state


def report_answer(packet):
    """Create controlled supported/incomplete entries accounting for every input fact.

    Args: packet: Exact production report payload.
    Returns: Schema-shaped answer with a missing field on the second entry.
    """
    entries=[]
    for i,fact in enumerate(packet['facts']):
        entries.append({'label':fact['fact']['statement'],'label_fact_ids':[fact['id']],
            'fields':[{'requirement_id':r['id'],'status':'unknown' if i==1 else 'supported',
                       'value':'' if i==1 else fact['fact']['statement'],'fact_ids':[] if i==1 else [fact['id']]} for r in packet['requirements']]})
    return {'summary':[{'text':'The collected evidence supports the following candidates.','fact_ids':[f['id'] for f in packet['facts']]}] if packet['facts'] else [],
            'entries':entries,'gaps':['One candidate lacks the requested field.'],'exclusions':[]}


class ReportCaller:
    """Controlled report output; transport ledger tested separately at API boundary."""
    model='offline-test'
    def __init__(self,invalid=False):
        """Configure valid versus fabricated-citation responses.

        Args: invalid: Whether to return unknown source IDs.
        Returns: None; initializes call ledger for test assertions.
        """
        self.calls=[];self.invalid=invalid
    def __call__(self,stage,request,sid):
        """Return a complete packet-derived fixture or deliberately invalid output.

        Args: stage: Actual label; request: Complete provider payload; sid: Run ID.
        Returns: JSON response; makes no provider calls.
        """
        self.calls.append((stage,request,sid));result=report_answer(json.loads(request['messages'][0]['content']))
        if self.invalid:result['summary'][0]['fact_ids']=['invented']
        return json.dumps(result)


class ReportTests(unittest.TestCase):
    """Keep Step1 requirements authoritative and every report assertion traceable."""
    def test_http_full_packet_report_csv_no_replay(self):
        """Exercise actual report route/graph, field completeness and exports.

        Args: None.
        Returns: None; asserts original preservation, full input and one model call.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=report_fixture(root);sid=state['session_id'];caller=ReportCaller();sessions=Sessions(root,caller);sessions.write(state)
            originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/sid).rglob('*') if p.is_file() and p.name!='session.json'}
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.get(f'/report/{sid}').status_code,200)
                    self.assertEqual(client.get(f'/api/setup/{sid}/report.csv').status_code,409)
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code,202)
                    client.post(f'/api/setup/{sid}/report')
                    for _ in range(500):
                        result=client.get(f'/api/setup/{sid}').json()
                        if result['status']not in ('report_running', 'link_ranking_running'):break
                        time.sleep(.01)
                    self.assertEqual(result['status'],'report_completed')
                    self.assertEqual(result['report']['facts_accounted'],2)
                    self.assertEqual(result['report']['complete_entries'],1);self.assertEqual(result['report']['incomplete_entries'],1)
                    report=client.get(f'/api/setup/{sid}/report-data').json();packet=json.loads(caller.calls[0][1]['messages'][0]['content'])
                    self.assertEqual([{k:v for k,v in r.items() if k!='id'} for r in packet['requirements']],state['information_needed'])
                    self.assertEqual(len(packet['facts']),2);self.assertEqual(report['requirements'],packet['requirements'])
                    self.assertTrue(all(f['evidence'] for f in packet['facts']))
                    export=client.get(f'/api/setup/{sid}/report.csv');self.assertEqual(export.status_code,200)
                    rows=list(csv.reader(StringIO(export.content.decode('utf-8-sig'))));self.assertEqual(len(rows),3);self.assertEqual(rows[2][1],'Incomplete candidate')
                    client.post(f'/api/setup/{sid}/report');self.assertEqual(len(caller.calls),1)
                    for p,digest in originals.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
            finally:sessions.close()

    def test_explicit_retry_preserves_failure_and_blocks_stale_replay(self):
        """Retry only the named failed attempt and retain its artifacts.

        Args: None.
        Returns: None; asserts two calls, history preservation and no stale replay.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=report_fixture(root);sid=state['session_id'];caller=ReportCaller(invalid=True)
            sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    for _ in range(500):
                        failed=sessions.read(sid)
                        if failed['status']not in ('report_running', 'link_ranking_running'):break
                        time.sleep(.01)
                    self.assertEqual(failed['status'],'report_failed')
                    # Terminal progress can precede the final summary write. Wait
                    # for the queued worker to finish before snapshotting its files.
                    sessions.executor.submit(lambda: None).result(timeout=5)
                    prior=failed['report'];folder=root/sid/prior['artifact_directory']
                    hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
                    caller.invalid=False;payload={'previous_started_at':prior['started_at']}
                    self.assertEqual(client.post(f'/api/setup/{sid}/report/retry',json=payload).status_code,202)
                    client.post(f'/api/setup/{sid}/report/retry',json=payload)
                    for _ in range(500):
                        result=sessions.read(sid)
                        if result['status']not in ('report_running', 'link_ranking_running'):break
                        time.sleep(.01)
                    self.assertEqual(result['status'],'report_completed')
                    self.assertEqual(result['report_history'],[prior])
                    client.post(f'/api/setup/{sid}/report/retry',json=payload)
                    self.assertEqual(len(caller.calls),2)
                    self.assertEqual(caller.calls[-1][1]['max_tokens'],32768)
                    self.assertEqual(caller.calls[-1][1]['timeout'],180)
                    for p,digest in hashes.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
            finally:sessions.close()

    def test_missing_fields_ids_and_unaccounted_facts_fail(self):
        """Reject dropped requirements, invented citations and hidden fact omissions.

        Args: None.
        Returns: None; valid complete output passes, every malformed variant fails.
        """
        packet={'requirements':[{'id':'R1'}],'facts':[{'id':'F1','fact':{'statement':'Company'}},{'id':'F2','fact':{'statement':'Other'}}]}
        good=report_answer(packet);validate_report(json.dumps(good),packet)
        variants=[]
        v=copy.deepcopy(good);v['entries'][0]['fields']=[];variants.append(v)
        v=copy.deepcopy(good);v['entries'][0]['fields'][0]['fact_ids']=[];variants.append(v)
        v=copy.deepcopy(good);v['entries'][1]['fields'][0]['value']='invented';variants.append(v)
        v=copy.deepcopy(good);v['entries'][1]['fields'][0].update(status='insufficient',value='No evidence available',fact_ids=[]);variants.append(v)
        v=copy.deepcopy(good);v['entries'][0]['label_fact_ids']=['invented'];variants.append(v)
        v=copy.deepcopy(good);v['entries']=v['entries'][:1];v['summary']=[];variants.append(v)
        v=copy.deepcopy(good);v['exclusions']=[{'fact_id':'invented','reason':'Unused'}];variants.append(v)
        for v in variants:
            with self.assertRaises(ValueError):validate_report(json.dumps(v),packet)

    def test_failure_empty_stop_and_formula_safe_csv(self):
        """No invalid/empty/cancelled report is mistaken for a valid generated answer.

        Args: None.
        Returns: None; checks failed report withholding, zero-call empty/stop cases
            and spreadsheet formula escaping without changing displayed data.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=report_fixture(root);caller=ReportCaller(invalid=True)
            worker=ReportRun(root/state['session_id'],state,lambda _:None,caller,Event());worker.execute()
            self.assertEqual(state['status'],'report_failed');self.assertFalse((worker.root/'report.json').exists())
            stop=Event();stop.set();worker=ReportRun(root/state['session_id'],state,lambda _:None,caller,stop);worker.execute();self.assertEqual(state['status'],'report_cancelled');self.assertEqual(len(caller.calls),1)
            path=root/state['session_id']/state['investigation']['artifact_directory']/'report_knowledge.json'
            data=json.loads(path.read_text());data['facts']=[];path.write_text(json.dumps(data));state['investigation']['report_knowledge']=0
            worker=ReportRun(root/state['session_id'],state,lambda _:None,caller,Event());worker.execute();self.assertEqual(state['status'],'report_completed');self.assertEqual(len(caller.calls),1)
        report={'requirements':[{'id':'R1','name':'Name'}],'entries':[{'label':'=BAD()','fields':[{'requirement_id':'R1','status':'supported','value':'@formula','fact_ids':['F1']}]}]}
        row=list(csv.reader(StringIO(report_csv(report))))[1];self.assertTrue(row[0].startswith("'="));self.assertTrue(row[2].startswith("'@"))
