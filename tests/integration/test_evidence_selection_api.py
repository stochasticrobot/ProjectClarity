"""Check the web action that selects useful facts from saved extraction results.

The tests create source-backed facts and supply controlled AI selections, then
use the application's web interface to start and inspect selection. They check
that the right question and facts are sent, selected originals stay intact, and
failures or repeated actions do not silently lose or duplicate work. Real model
relevance judgments are outside these tests.
"""
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from projectclarity.evidence_selection.evidence_selection_stage import EvidenceSelectionStage
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.knowledge_extraction.knowledge_extraction import extraction_request
from projectclarity.ux.web_app import Sessions, create_app


class SelectorCaller:
    """Capture full requests and return controlled batch-level decisions."""
    model='offline-test'

    def __init__(self, modes=None, stop=None):
        """Initialize test responses without model calls.

        Args:
            modes: Ordered selected, none, or invalid decisions.
            stop: Optional cancellation event set during a call.
        Returns: None; initializes an empty call ledger.
        """
        self.modes,self.stop,self.calls=modes or ['selected'],stop,[]

    def __call__(self, stage, request, sid):
        """Supply fixture decisions to the real selection validator.

        Args:
            stage: Model receipt label.
            request: Full actual selection payload.
            sid: Owning session UUID.
        Returns: Decision JSON; optionally sets cancellation after receiving input.
        """
        if stage.startswith('report-'):
            self.calls.append((stage,request,sid))
            data=json.loads(request['messages'][0]['content'])
            return json.dumps({'summary':[],'entries':[],'gaps':['Fixture contains no complete answer.'],
                'exclusions':[{'fact_id':f['id'],'reason':'Fixture excluded.'} for f in data['facts']]})
        mode=self.modes[min(len(self.calls),len(self.modes)-1)]
        self.calls.append((stage,request,sid))
        if self.stop:self.stop.set()
        facts=json.loads(request['messages'][0]['content'])['facts']
        if stage.startswith('triage-'):
            return json.dumps({'decisions':[{'fact_id':f['fact_id'],'bucket':'report_knowledge','reason':'Supported by the original quote.'} for f in facts]})
        return json.dumps({'selected_facts':[] if mode=='none' else [{'fact_id':'invented' if mode=='invalid' else facts[0]['fact_id'],'reason':'Provides Canadian AI activity evidence.'}]})


def seed(root, empty=False, count=3):
    """Save source-backed extraction receipts and their immutable index.

    Args:
        root: Temporary session root.
        empty: Whether the fixtures should contain zero facts.
        count: Number of independent chunk fixtures; defaults to three.
    Returns: Historical session state and original file hashes for preservation checks.
    """
    sid=str(uuid4());folder=root/sid/'run-old';folder.mkdir(parents=True)
    requirements=[{'kind':'entity_type','name':'Company','description':'Identify Canadian AI companies.'}]
    refs=[];originals={}
    for i in range(count):
        statements=[f'Company {i} builds AI tools in Canada.',f'Company {i} has a garden.']
        text=' '.join(statements)
        facts=[{'subject_id':'e','predicate':'states','value':s,'object_id':None,'statement':s,'quote':s} for s in statements]
        parsed={'entities':[{'id':'e','name':f'Company {i}','type':'organization','subtype':None}],'facts':[] if empty else facts}
        path=folder/f'chunk-{i}.json'
        save(path,{'status':'checks_pass','parsed':parsed,'request':extraction_request(text,requirements,'offline-test',8192)},'')
        digest=hashlib.sha256(path.read_bytes()).hexdigest();originals[path]=digest
        refs.append({'source_url':f'https://example.org/{i}','document_id':i+1,'chunk_index':0,'artifact':path.name,'sha256':digest,'facts':len(parsed['facts'])})
    state={'session_id':sid,'original_question':'Canadian AI?','research_question':'Canadian AI companies?',
        'information_needed':requirements,'selection_concurrency':1,'status':'run_partial','run':{'status':'partial','artifact_directory':'run-old','sources':[],'started_at':'old'}}
    save(folder/'knowledge.json',{'research_question':state['research_question'],'information_needed':requirements,'chunks':refs,'facts':sum(r['facts'] for r in refs)},'')
    originals[folder/'knowledge.json']=hashlib.sha256((folder/'knowledge.json').read_bytes()).hexdigest()
    return state,originals


def settle(client,sid):
    """Wait for the actual offline worker to reach a terminal state.

    Args:
        client: TestClient for production routes.
        sid: Session UUID.
    Returns: Terminal snapshot, or raises after five seconds.
    """
    for _ in range(500):
        state=client.get(f'/api/setup/{sid}').json()
        if state['status'] not in ('selection_running','triage_running','investigation_running','link_ranking_running','report_running'):return state
        time.sleep(.01)
    raise AssertionError('Selection did not settle')


class EvidenceTests(unittest.TestCase):
    """Verify real entry points, exact handoff, failures, persistence and no replay."""

    def test_saved_handoff_partial_and_no_replay(self):
        """Send all chunks through the HTTP entry and real graph with mixed decisions.

        Args: None.
        Returns: None; verifies exact inputs, records, honest counts and idempotency.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,originals=seed(root);caller=SelectorCaller(['selected','none','invalid'])
            sessions=Sessions(root,caller);sessions.write(state);sid=state['session_id']
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.get(f'/evidence/{sid}').status_code,200)
                    self.assertEqual(client.post(f'/api/setup/{sid}/selection').status_code,202)
                    client.post(f'/api/setup/{sid}/selection')
                    result=settle(client,sid);s=result['selection']
                    self.assertEqual(result['selection']['status'],'partial')
                    self.assertEqual((s['facts_total'],s['facts_reviewed'],s['facts_selected']),(6,4,1))
                    self.assertEqual([b['status'] for b in s['batches']],['completed','completed','failed'])
                    self.assertIsNone(s['batches'][2]['selected'])
                    selection_calls=[c for c in caller.calls if c[0].startswith('evidence-')]
                    self.assertEqual(len(selection_calls),3)
                    for i,(_,request,_) in enumerate(selection_calls):
                        payload=json.loads(request['messages'][0]['content'])
                        self.assertEqual(payload['RESEARCH QUESTION'],state['research_question'])
                        self.assertEqual(payload['REQUESTED INFORMATION'],state['information_needed'])
                        original=json.loads((root/sid/'run-old'/f'chunk-{i}.json').read_text())['parsed']
                        self.assertEqual([f['fact'] for f in payload['facts']],original['facts'])
                        self.assertEqual(payload['entities'],original['entities'])
                    data=client.get(f'/api/setup/{sid}/evidence').json()
                    self.assertEqual(len(data['batches']),2)
                    self.assertEqual(sum(f['selected'] for b in data['batches'] for f in b['facts']),1)
                    store=json.loads((root/sid/s['artifact_directory']/'relevant_knowledge.json').read_text())
                    self.assertEqual(store['facts'][0]['fact'],data['batches'][0]['facts'][0]['fact'])
                    self.assertEqual(store['upstream_status'],'partial')
                    client.post(f'/api/setup/{sid}/selection');client.get(f'/evidence/{sid}')
                    self.assertEqual(len([c for c in caller.calls if c[0].startswith('evidence-')]),3)
                    for path,digest in originals.items():self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            finally:sessions.close()

    def test_cancel_keeps_completed_batch(self):
        """Stop during a model call without discarding it or scheduling more work.

        Args: None.
        Returns: None; accepted facts survive and remaining batches stay pending.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=seed(root);stop=Event();caller=SelectorCaller(stop=stop)
            runner=EvidenceSelectionStage(root/state['session_id'],state,lambda _:None,caller,stop);runner.execute()
            s=state['selection']
            self.assertEqual((s['status'],s['facts_reviewed'],s['facts_selected']),('cancelled',2,1))
            self.assertEqual([b['status'] for b in s['batches']],['completed','pending','pending'])
            self.assertEqual(len(json.loads((runner.root/'relevant_knowledge.json').read_text())['facts']),1)
            self.assertEqual(len(caller.calls),1)

    def test_changed_artifact_skips_model(self):
        """Reject changed extraction bytes while processing untouched later batches.

        Args: None.
        Returns: None; invalid input remains unreviewed, not a successful exclusion.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=seed(root);(root/state['session_id']/'run-old/chunk-0.json').write_text('{}')
            caller=SelectorCaller();runner=EvidenceSelectionStage(root/state['session_id'],state,lambda _:None,caller,Event());runner.execute()
            self.assertEqual(len(caller.calls),2)
            self.assertEqual(state['selection']['facts_reviewed'],4)
            self.assertEqual(state['selection']['status'],'partial')

    def test_empty_batches_need_no_calls(self):
        """Accept legitimate empty knowledge without confusing it with failed input.

        Args: None.
        Returns: None; all empty batches succeed without model requests.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=seed(root,empty=True);caller=SelectorCaller()
            EvidenceSelectionStage(root/state['session_id'],state,lambda _:None,caller,Event()).execute()
            self.assertEqual(state['selection']['status'],'completed')
            self.assertEqual(state['selection']['batches_passed'],3)
            self.assertEqual(caller.calls,[])

    def test_restart_interrupted_no_replay(self):
        """Recover running snapshots without automatically repeating model work.

        Args: None.
        Returns: None; interrupted status and original receipts survive reload/start.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=seed(root);caller=SelectorCaller()
            EvidenceSelectionStage(root/state['session_id'],state,lambda _:None,caller,Event())
            state['status']='selection_running';save(root/state['session_id']/'session.json',state,'')
            sessions=Sessions(root,caller)
            try:
                result=sessions.start_selection(state['session_id'])
                self.assertEqual(result['status'],'selection_interrupted')
                self.assertEqual(result['selection']['status'],'interrupted')
                self.assertEqual(caller.calls,[])
            finally:sessions.close()

    def test_stop_api_and_active_work_exclusion(self):
        """Stop through the real API while a provider call is in flight.

        Args: None.
        Returns: None; verifies no duplicate work, no concurrent run and retained result.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=seed(root);entered=Event();release=Event()
            class PausedCaller(SelectorCaller):
                """Hold only the provider boundary so API behavior can be inspected."""
                def __call__(self, stage, request, sid):
                    """Pause until the test has signalled cancellation through HTTP.

                    Args:
                        stage: Receipt name.
                        request: Complete selection input.
                        sid: Session UUID.
                    Returns: Valid decision after release; times out on test failure.
                    """
                    entered.set()
                    if not release.wait(5):raise AssertionError('Test did not release provider')
                    return super().__call__(stage,request,sid)
            caller=PausedCaller();sessions=Sessions(root,caller);sessions.write(state);sid=state['session_id']
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/selection')
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(client.post(f'/api/setup/{sid}/selection').status_code,202)
                    conflict=client.post('/api/setup',json={'session_id':str(uuid4()),'question':'Another question'})
                    self.assertEqual(conflict.status_code,409)
                    self.assertTrue(client.post(f'/api/setup/{sid}/stop').json()['stop_requested'])
                    release.set();result=settle(client,sid)
                    self.assertEqual(result['status'],'selection_cancelled')
                    self.assertEqual(result['selection']['facts_reviewed'],2)
                    self.assertEqual(len(caller.calls),1)
            finally:
                release.set();sessions.close()
