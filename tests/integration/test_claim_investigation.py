"""Check that reviewing a doubtful claim uses saved evidence and keeps honest outcomes.

The tests create documents with missing details, nearby supporting text and
controlled AI replies. They exercise passage collection and the web action that
starts investigation, checking accepted, rejected and unreviewed results. No real
AI service is contacted; these checks protect handling of evidence, not AI judgment.
"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from fastapi.testclient import TestClient
from projectclarity.claim_review.claim_evidence_collection import EvidenceCollector
from projectclarity.claim_review.claim_investigation import InvestigationRun, validate_investigation
from projectclarity.claim_review.claim_triage import TriageRun
from tests.integration.test_claim_triage import selected_fixture, TriageCaller
from projectclarity.ux.web_app import Sessions, create_app


def investigation_fixture(root, all_flagged=False, count=3):
    """Produce real selection/triage stores, plus saved full document context.

    Args: root: Temporary session root; all_flagged: Flag all selected facts;
        count: Number of independent source groups, default three.
    Returns: Session state with saved passages, including text extraction missed.
    """
    state,_=selected_fixture(root,count=count)
    state['investigation_concurrency']=1
    folder=root/state['session_id']/'run-old'
    for i in range(count):
        receipt=json.loads((folder/f'chunk-{i}.json').read_text())
        text=json.loads(receipt['request']['messages'][0]['content'])['document_chunk']
        parts=[text,' Its headquarters are in Canada.',' unrelated garden notes. ',f'Company {i} headquarters opened in Toronto in 2020.']
        if i==2:parts[-1]=' Company 0 headquarters moved to Halifax in 2024.'
        complete=''.join(parts);offset=0;chunks=[]
        for index,part in enumerate(parts):
            chunks.append({'index':index,'start':offset,'end':offset+len(part),'pages':[index+1],'status':'checks_pass' if index==0 else 'failed','facts':2 if index==0 else 0});offset+=len(part)
        doc=folder/f'document-{i+1:03d}';doc.mkdir();(doc/'document.txt').write_text(complete)
        state['run']['sources'].append({'id':i+1,'url':f'https://example.org/{i}','final_url':f'https://example.org/{i}',
            'parsed':True,'text_sha256':hashlib.sha256(complete.encode()).hexdigest(),'chunks':chunks,'status':'extraction_partial'})
    caller=TriageCaller(['needs_verification'] if all_flagged else ['needs_verification','needs_verification','report_knowledge'])
    TriageRun(root/state['session_id'],state,lambda _:None,caller,Event()).execute()
    return state


class InvestigationCaller:
    """External-I/O-only substitute; real collection, schema and routing stay active."""
    model='offline-test'
    def __init__(self, modes=None, stop=None):
        """Configure controlled review outcomes.

        Args: modes: Ordered accept/reject/invalid/raise outcomes; stop: Optional event.
        Returns: None; initializes request ledger without making calls.
        """
        self.modes=modes or ['accept','reject'];self.stop=stop;self.calls=[]
    def __call__(self, stage, request, sid):
        """Return a decision on the exact supplied fact or a deliberate failure.

        Args: stage: Receipt label; request: Production payload; sid: Session ID.
        Returns: Raw decision JSON or RuntimeError; may request cooperative stop.
        """
        if stage.startswith('report-'):
            self.calls.append((stage,request,sid))
            data=json.loads(request['messages'][0]['content'])
            if 'items' in request['output_config']['format']['schema']['properties']:
                return json.dumps({'items':[],'unused_fact_ids':[f['id'] for f in data['facts']]})
            return json.dumps({'summary':[],'entries':[],'gaps':['Fixture contains no complete answer.'],
                'exclusions':[{'fact_id':f['id'],'reason':'Fixture excluded.'} for f in data['facts']]})
        data=json.loads(request['messages'][0]['content']);mode=self.modes[min(len(self.calls),len(self.modes)-1)]
        self.calls.append((stage,request,sid))
        if self.stop:self.stop.set()
        if mode=='raise':raise RuntimeError('Transport fixture failed')
        passage=data['passages'][0]
        return json.dumps({'fact_id':data['claim']['fact_id'],'decision':'accept' if mode in ('accept','invalid') else 'reject',
            'reason':'Original wording is supported.' if mode=='accept' else 'Missing information is unresolved.',
            'citations':[{'passage_id':passage['passage_id'],'quote':'invented quote' if mode=='invalid' else passage['text']}] if mode in ('accept','invalid') else []})


def wait_terminal(client,sid):
    """Wait briefly for the production executor with fake model calls.

    Args: client: Actual HTTP test client; sid: Session ID.
    Returns: Terminal public snapshot or AssertionError after five seconds.
    """
    for _ in range(500):
        state=client.get(f'/api/setup/{sid}').json()
        if state['status'] not in ('investigation_running','link_ranking_running','report_running'):return state
        time.sleep(.01)
    raise AssertionError('Investigation did not finish')


class InvestigationTests(unittest.TestCase):
    """Validate retrieval coverage, report routing and immutable source records."""
    def test_retrieval_subject_scope_neighbors_fulltext_budget(self):
        """Find source text missed by extraction without merging chunk-local identities.

        Args: None.
        Returns: None; checks ranking, spans, page IDs and explicit budget omissions.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=investigation_fixture(root)
            worker=InvestigationRun(root/state['session_id'],state,lambda _:None,InvestigationCaller(),Event())
            collector=EvidenceCollector(root/state['session_id'],state,lambda:None)
            record=worker.flagged[0];record['triage_reason']='Missing headquarters location and date.'
            packet=collector.collect(record)
            ids=[p['passage_id'] for p in packet['passages']]
            self.assertEqual(ids[:2],['D1C0','D1C1']);self.assertIn('D1C3',ids);self.assertIn('D3C3',ids)
            self.assertLess(ids.index('D1C3'),ids.index('D3C3'))
            self.assertNotIn('D2C0',ids) # same local entity ID e is NOT cross-document identity
            self.assertIn('headquartered',packet['retrieval']['gap_terms'])
            self.assertIn('year',packet['retrieval']['gap_terms'])
            for passage in packet['passages']:
                text=(root/state['session_id']/'run-old'/f'document-{passage["document_id"]:03d}'/'document.txt').read_text()
                self.assertEqual(passage['text'],text[passage['start_char']:passage['end_char']])
                self.assertTrue(passage['pages'])
            bounded=collector.collect(record,budget=len(packet['passages'][0]['text']))
            self.assertEqual(len(bounded['passages']),1);self.assertIn('D1C1',bounded['retrieval']['omitted_passage_ids'])
            with self.assertRaises(ValueError):collector.collect(record,budget=1)

    def test_http_route_report_and_failures_no_replay(self):
        """Exercise production HTTP start and graph, including invalid citation failure.

        Args: None.
        Returns: None; validates exact packets, distinct outcomes, immutable inputs,
            preserved existing Report Knowledge and idempotent starts/refresh.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=investigation_fixture(root,all_flagged=True);sid=state['session_id']
            baseline={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/sid).rglob('*') if p.is_file()}
            caller=InvestigationCaller(['accept','reject','invalid']);sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.get(f'/investigation/{sid}').status_code,200)
                    preview=client.get(f'/api/setup/{sid}/investigation-records').json()
                    self.assertEqual(len(preview['records']),3);self.assertTrue(all(r['status']=='pending' for r in preview['records']))
                    self.assertEqual(caller.calls,[])
                    self.assertEqual(client.post(f'/api/setup/{sid}/investigation').status_code,202)
                    client.post(f'/api/setup/{sid}/investigation')
                    result=wait_terminal(client,sid);progress=result['investigation']
                    self.assertEqual(result['status'],'report_completed')
                    self.assertEqual(result['investigation']['status'],'partial')
                    self.assertEqual([progress[k] for k in ('claims_total','claims_finished','accepted','rejected','failed','report_knowledge')],[3,3,1,1,1,1])
                    records=client.get(f'/api/setup/{sid}/investigation-records').json()
                    self.assertEqual([r['status'] for r in records['records']],['accepted','rejected','failed'])
                    self.assertEqual(len(records['report_facts']),1)
                    folder=root/sid/progress['artifact_directory']
                    for i,(_,request,_) in enumerate([c for c in caller.calls if c[0].startswith('investigate-')],1):
                        data=json.loads(request['messages'][0]['content'])
                        saved_packet=json.loads((folder/f'packet-{i:04d}.json').read_text());self.assertFalse(saved_packet.pop('credential_redaction_applied',False))
                        self.assertEqual(data,saved_packet)
                        self.assertEqual(data['research_question'],state['research_question'])
                        self.assertEqual(data['requested_information'],state['information_needed'])
                        self.assertEqual(request['system'],Path('src/projectclarity/prompts/investigate_claims.txt').read_text())
                    self.assertEqual(len(json.loads((folder/'rejected.json').read_text())['facts']),1)
                    self.assertEqual(len(json.loads((folder/'needs_review.json').read_text())['facts']),1)
                    client.post(f'/api/setup/{sid}/investigation');client.get(f'/investigation/{sid}')
                    self.assertEqual(len(caller.calls),4)
                    for p,digest in baseline.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
            finally:sessions.close()

    def test_existing_report_preserved_and_stop_during_call(self):
        """Retain an in-flight accepted result and baseline report facts when stopping.

        Args: None.
        Returns: None; pending claims stay unreviewed and no second request is sent.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=investigation_fixture(root);stop=Event();caller=InvestigationCaller(stop=stop)
            worker=InvestigationRun(root/state['session_id'],state,lambda _:None,caller,stop);baseline=list(worker.baseline)
            worker.execute()
            self.assertEqual(state['status'],'investigation_cancelled');self.assertEqual(len(caller.calls),1)
            self.assertEqual([r['status'] for r in worker.rows],['accepted','pending'])
            report=json.loads((worker.root/'report_knowledge.json').read_text())['facts']
            self.assertEqual(report[:len(baseline)],baseline);self.assertEqual(len(report),2)

    def test_identity_quote_validation_and_changed_source(self):
        """Reject fabricated citations/IDs and altered originals before report routing.

        Args: None.
        Returns: None; no model calls for a tampered original; no false acceptance.
        """
        packet={'claim':{'fact_id':'F1'},'passages':[{'passage_id':'P1','text':'Acme is in Canada.'}]}
        good={'fact_id':'F1','decision':'accept','reason':'Supported.','citations':[{'passage_id':'P1','quote':'Acme is in Canada.'}]}
        bad=[{**good,'fact_id':'F2'},{**good,'reason':''},{**good,'citations':[]},
             {**good,'citations':[{'passage_id':'wrong','quote':'Acme'}]},
             {**good,'citations':[{'passage_id':'P1','quote':'Canada headquarters'}]}]
        for item in bad:
            with self.assertRaises(ValueError):validate_investigation(json.dumps(item),packet)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=investigation_fixture(root);(root/state['session_id']/'run-old/document-001/document.txt').write_text('changed')
            caller=InvestigationCaller();worker=InvestigationRun(root/state['session_id'],state,lambda _:None,caller,Event());worker.execute()
            self.assertEqual(worker.rows[0]['status'],'failed');self.assertEqual(len(caller.calls),1)

    def test_empty_and_restart_no_replay(self):
        """Complete an empty flagged bucket without calls; interrupt uncertain work.

        Args: None.
        Returns: None; verifies no automatic retry after server restart.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=selected_fixture(root)
            state['investigation_concurrency']=1
            TriageRun(root/state['session_id'],state,lambda _:None,TriageCaller(['report_knowledge']),Event()).execute()
            caller=InvestigationCaller();worker=InvestigationRun(root/state['session_id'],state,lambda _:None,caller,Event());worker.execute()
            self.assertEqual(state['status'],'investigation_completed');self.assertEqual(caller.calls,[])
            self.assertEqual(worker.progress['report_knowledge'],3)
            state['status']='investigation_running';state['investigation']['status']='running'
            (root/state['session_id']/'session.json').write_text(json.dumps(state))
            sessions=Sessions(root,caller)
            try:
                self.assertEqual(sessions.read(state['session_id'])['status'],'investigation_interrupted')
                self.assertEqual(sessions.start_investigation(state['session_id'])['status'],'investigation_interrupted')
                self.assertEqual(caller.calls,[])
            finally:sessions.close()
