"""Check that every selected claim is either routed for review, accepted or left unreviewed.

Controlled AI replies include valid decisions and incomplete answers. The tests
use the application's triage and web actions to check that original text reaches
the reviewer, decisions are saved and failures do not promote claims into the
report. They do not measure how well a real model judges source support.
"""
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from fastapi.testclient import TestClient
from projectclarity.evidence_selection.evidence_selection_stage import EvidenceSelectionStage
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.claim_review.claim_triage import TriageRun, validate_decisions
from tests.integration.test_evidence_selection_api import seed, SelectorCaller
from projectclarity.ux.web_app import Sessions, create_app


def selected_fixture(root, empty=False, count=3):
    """Create source-backed selected knowledge through the ordinary selector graph.

    Args:
        root: Temporary session root.
        empty: Whether to select no facts.
        count: Number of original chunk groups, default three.
    Returns: Session snapshot and original artifact hashes for preservation checks.
    """
    state, hashes=seed(root,count=count)
    state['triage_concurrency']=1
    EvidenceSelectionStage(root/state['session_id'],state,lambda _:None,SelectorCaller(['none'] if empty else ['selected']),Event()).execute()
    path=root/state['session_id']/state['selection']['artifact_directory']/'relevant_knowledge.json'
    hashes[path]=hashlib.sha256(path.read_bytes()).hexdigest()
    return state,hashes


class TriageCaller:
    """Controlled semantic decisions; the real validator and routing remain in use."""
    model='offline-test'

    def __init__(self, modes=None, stop=None):
        """Configure batch responses without making external calls.

        Args:
            modes: Ordered routing buckets or invalid responses.
            stop: Optional cancellation event set during the first model call.
        Returns: None; initializes captured requests.
        """
        self.modes=modes or ['needs_verification','report_knowledge','invalid'];self.stop=stop;self.calls=[]

    def __call__(self, stage, request, sid):
        """Supply routing JSON or an intentionally incomplete response.

        Args:
            stage: Ordinary model receipt label.
            request: Exact production input containing facts/source context.
            sid: Owning session UUID.
        Returns: Decision JSON; may signal cancellation after accepting the input.
        """
        if stage.startswith('report-'):
            self.calls.append((stage,request,sid))
            data=json.loads(request['messages'][0]['content'])
            if 'items' in request['output_config']['format']['schema']['properties']:
                return json.dumps({'items':[],'unused_fact_ids':[f['id'] for f in data['facts']]})
            return json.dumps({'summary':[],'entries':[],'gaps':['Fixture contains no complete answer.'],
                'exclusions':[{'fact_id':f['id'],'reason':'Fixture excluded.'} for f in data['facts']]})
        mode=self.modes[min(len(self.calls),len(self.modes)-1)];self.calls.append((stage,request,sid))
        if self.stop:self.stop.set()
        data=json.loads(request['messages'][0]['content'])
        if stage.startswith('investigate-'):
            return json.dumps({'fact_id':data['claim']['fact_id'],'decision':'reject','reason':'Missing date is unresolved.','citations':[]})
        facts=data['facts']
        return json.dumps({'decisions':[] if mode=='invalid' else [{'fact_id':f['fact_id'],'bucket':mode,'reason':'Check the omitted date.' if mode=='needs_verification' else 'The assertion and its quote preserve the full source statement.'} for f in facts]})


def settled(client,sid):
    """Wait for offline model fixtures to finish through the production executor.

    Args:
        client: Actual API TestClient.
        sid: Saved session UUID.
    Returns: Terminal snapshot; fails after five seconds rather than hanging.
    """
    for _ in range(500):
        state=client.get(f'/api/setup/{sid}').json()
        if state['status'] not in ('triage_running','investigation_running','link_ranking_running','report_running'):return state
        time.sleep(.01)
    raise AssertionError('Triage did not finish')


class TriageTests(unittest.TestCase):
    """Verify complete routing, failed/unreviewed distinction and immutable inputs."""

    def test_http_handoff_exact_inputs_buckets_and_no_replay(self):
        """Exercise all three batches, including a failed judgment, via real routes.

        Args: None.
        Returns: None; validates full source context, saved originals and bucket counts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,hashes=selected_fixture(root);caller=TriageCaller();sessions=Sessions(root,caller);sessions.write(state);sid=state['session_id']
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.get(f'/triage/{sid}').status_code,200)
                    self.assertEqual(client.post(f'/api/setup/{sid}/triage').status_code,202)
                    client.post(f'/api/setup/{sid}/triage')
                    result=settled(client,sid);t=result['triage']
                    self.assertEqual(result['status'],'report_completed')
                    self.assertEqual(result['triage']['status'],'partial')
                    self.assertEqual((t['facts_total'],t['facts_reviewed'],t['needs_verification'],t['report_knowledge']),(3,2,1,1))
                    self.assertEqual([b['status'] for b in t['batches']],['completed','completed','failed'])
                    for i,(_,request,_) in enumerate(c for c in caller.calls if c[0].startswith('triage-')):
                        data=json.loads(request['messages'][0]['content'])
                        original=json.loads((root/sid/'run-old'/f'chunk-{i}.json').read_text())
                        self.assertEqual(data['research_question'],state['research_question'])
                        self.assertEqual(data['requested_information'],state['information_needed'])
                        self.assertEqual(data['document_chunk'],json.loads(original['request']['messages'][0]['content'])['document_chunk'])
                        self.assertEqual(data['entities'],original['parsed']['entities'])
                        self.assertEqual(data['facts'][0]['fact'],original['parsed']['facts'][0])
                    payload=client.get(f'/api/setup/{sid}/triage-records').json()
                    self.assertEqual(len(payload['facts']),2);self.assertEqual(len(payload['unreviewed_fact_ids']),1)
                    folder=root/sid/t['artifact_directory']
                    for name in ('needs_verification','report_knowledge'):
                        bucket=json.loads((folder/(name+'.json')).read_text())
                        self.assertEqual(len(bucket['facts']),1)
                        self.assertEqual(bucket['facts'][0]['triage_bucket'],name)
                    client.post(f'/api/setup/{sid}/triage');client.get(f'/triage/{sid}')
                    self.assertEqual(len(caller.calls),5)
                    for path,digest in hashes.items():self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            finally:sessions.close()

    def test_reject_missing_duplicate_unknown_and_blank_decisions(self):
        """Demand exactly one reasoned decision per input, without accepting omissions.

        Args: None.
        Returns: None; invalid responses raise before any fact can be promoted.
        """
        good={'fact_id':'one','bucket':'report_knowledge','reason':'Supported.'}
        for items in ([],[good,good],[{**good,'fact_id':'other'}],[{**good,'reason':' '}],[{**good,'bucket':'verified'}]):
            with self.assertRaises(ValueError):validate_decisions(json.dumps({'decisions':items}),[{'fact_id':'one'}])

    def test_cancel_retains_decisions_without_routing_pending_facts(self):
        """Retain an in-flight valid result and stop before the following batch.

        Args: None.
        Returns: None; remaining facts are unreviewed, not Report Knowledge.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=selected_fixture(root);stop=Event();caller=TriageCaller(stop=stop)
            worker=TriageRun(root/state['session_id'],state,lambda _:None,caller,stop);worker.execute()
            t=state['triage'];self.assertEqual(t['status'],'cancelled');self.assertEqual(t['facts_reviewed'],1)
            self.assertEqual(t['report_knowledge'],0)
            self.assertEqual(len(json.loads((worker.root/'decisions.json').read_text())['unreviewed_fact_ids']),2)
            self.assertEqual(len(caller.calls),1)

    def test_empty_selection_no_calls(self):
        """Complete an empty selected store without inventing decisions or calls.

        Args: None.
        Returns: None; both output buckets exist and are empty.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=selected_fixture(root,empty=True);caller=TriageCaller()
            worker=TriageRun(root/state['session_id'],state,lambda _:None,caller,Event());worker.execute()
            self.assertEqual(state['triage']['status'],'completed');self.assertEqual(caller.calls,[])
            self.assertEqual(json.loads((worker.root/'report_knowledge.json').read_text())['facts'],[])

    def test_changed_source_is_not_sent_to_model(self):
        """Fail altered provenance before a model can approve its records.

        Args: None.
        Returns: None; untouched later batches continue while altered input stays unreviewed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=selected_fixture(root)
            (root/state['session_id']/'run-old/chunk-0.json').write_text('{}')
            caller=TriageCaller(['report_knowledge']);TriageRun(root/state['session_id'],state,lambda _:None,caller,Event()).execute()
            self.assertEqual(state['triage']['facts_reviewed'],2);self.assertEqual(len(caller.calls),2)
            self.assertEqual(state['triage']['status'],'partial')

    def test_restart_marks_interrupted_without_replaying(self):
        """Recover a crashed triage session as explicitly interrupted.

        Args: None.
        Returns: None; repeated start leaves saved work intact and makes no calls.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=selected_fixture(root);caller=TriageCaller()
            TriageRun(root/state['session_id'],state,lambda _:None,caller,Event())
            state['status']='triage_running';save(root/state['session_id']/'session.json',state,'')
            sessions=Sessions(root,caller)
            try:
                self.assertEqual(sessions.start_triage(state['session_id'])['status'],'triage_interrupted')
                self.assertEqual(caller.calls,[])
            finally:sessions.close()
