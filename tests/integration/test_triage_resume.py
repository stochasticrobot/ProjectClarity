"""Check that stopped claim review can continue without repeating saved work.

The tests create a partly finished triage run, including a case where an AI reply
was saved just before processing stopped. They resume through the normal web action
and check that completed replies are reused, pending work continues, old files stay
unchanged and stale or repeated resume clicks do not duplicate work. AI replies
are controlled examples, not paid requests.
"""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from fastapi.testclient import TestClient
from tests.integration.test_claim_triage import selected_fixture,TriageCaller,settled
from projectclarity.claim_review.claim_triage import TriageRun
from projectclarity.ux.web_app import Sessions,create_app


def stopped_fixture(root, provider_saved=False):
    """Freeze a three-batch fixture at one completed and one interrupted batch.

    Args: root: Temporary store; provider_saved: Whether batch2 returned durably
        before its application-level receipt could be committed.
    Returns: Stopped state and hashes of all fixture evidence after simulated crash.
        Only temporary fixture files are altered, never real user artifacts.
    """
    state,_=selected_fixture(root);sid=state['session_id'];caller=TriageCaller(['report_knowledge'])
    worker=TriageRun(root/sid,state,lambda _:None,caller,Event());worker.execute()
    second=json.loads((worker.root/'batch-0002.json').read_text())
    for n in (2,3):(worker.root/f'batch-{n:04d}.json').unlink()
    if provider_saved:
        (root/sid/f'triage-{worker.root.name}-batch2-crash.json').write_text(json.dumps({'request':second['request'],'response':{'stop_reason':'end_turn'},'raw_text':second['raw_text']}))
    worker.decided=worker.decided[:1];state['status']='triage_interrupted';t=state['triage'];t.update(status='interrupted',facts_reviewed=1,report_knowledge=1,needs_verification=0,batches_finished=1,batches_passed=1,error='Server stopped')
    t['batches'][1]['status']='reviewing';t['batches'][2]['status']='pending';worker.write_buckets()
    hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/sid).rglob('*') if p.is_file()}
    return state,hashes


class ResumeTests(unittest.TestCase):
    """Require explicit eligible recovery and preserve prior evidence byte-for-byte."""
    def test_resume_pending_and_provider_saved_no_duplicates(self):
        """Rebuild completed decisions, reuse raw responses and continue downstream.

        Args: None.
        Returns: None; only pending batches call the model, repeat clicks do nothing.
        """
        for saved in (False,True):
            with TemporaryDirectory() as tmp:
                root=Path(tmp);state,hashes=stopped_fixture(root,saved);state['triage_concurrency']=6;sid=state['session_id'];old=copy.deepcopy(state['triage']);caller=TriageCaller(['report_knowledge']);sessions=Sessions(root,caller);sessions.write(state)
                try:
                    with TestClient(create_app(sessions)) as client:
                        payload={'artifact_directory':old['artifact_directory']}
                        self.assertEqual(client.post(f'/api/setup/{sid}/resume',json=payload).status_code,202)
                        client.post(f'/api/setup/{sid}/resume',json=payload)
                        result=settled(client,sid)
                        self.assertEqual(result['status'],'report_completed');self.assertEqual(result['triage']['facts_reviewed'],3)
                        self.assertEqual(result['triage']['reused_batches'],2 if saved else 1)
                        self.assertEqual(sum(c[0].startswith('triage-') for c in caller.calls),1 if saved else 2)
                        count=len(caller.calls);client.post(f'/api/setup/{sid}/resume',json=payload);self.assertEqual(len(caller.calls),count)
                        self.assertEqual(result['triage_history'],[old])
                        for p,digest in hashes.items():self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),digest)
                finally:sessions.close()

    def test_parallel_resume_keeps_prior_failures_unreviewed(self):
        """Do not turn concurrency into implicit retries of rejected decisions.

        Args: None.
        Returns: None; reuses two valid replies at concurrency six, leaves the
            failed group unreviewed and makes no model calls on recovery.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=selected_fixture(root);sid=state['session_id']
            first=TriageRun(root/sid,state,lambda _:None,TriageCaller(['invalid','report_knowledge']),Event())
            first.execute();prior=state['triage']['artifact_directory']
            state['triage_concurrency']=6;caller=TriageCaller(['report_knowledge'])
            resumed=TriageRun(root/sid,state,lambda _:None,caller,Event(),resume_from=prior)
            resumed.execute()
            self.assertEqual(state['triage']['status'],'partial')
            self.assertEqual(state['triage']['facts_reviewed'],2)
            self.assertEqual(state['triage']['reused_batches'],2)
            self.assertEqual(state['triage']['model_calls'],0)
            self.assertEqual(caller.calls,[])

    def test_ineligible_stale_and_changed_inputs(self):
        """Reject normal/stale resumes and prevent changed inputs from issuing calls.

        Args: None.
        Returns: None; only stopped attempts qualify; corrupt provenance fails closed.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=stopped_fixture(root);sid=state['session_id'];token=state['triage']['artifact_directory'];caller=TriageCaller();sessions=Sessions(root,caller)
            try:
                with TestClient(create_app(sessions)) as client:
                    for status in ('triage_completed','triage_failed','triage_running'):
                        state['status']=status;sessions.write(state)
                        self.assertEqual(client.post(f'/api/setup/{sid}/resume',json={'artifact_directory':token}).status_code,409)
                    state['status']='triage_cancelled';state['triage']['status']='cancelled';sessions.write(state)
                    self.assertEqual(client.post(f'/api/setup/{sid}/resume',json={'artifact_directory':'wrong'}).status_code,409)
                    path=root/sid/state['selection']['artifact_directory']/'relevant_knowledge.json';data=json.loads(path.read_text());data['research_question']='Changed';path.write_text(json.dumps(data))
                    self.assertEqual(client.post(f'/api/setup/{sid}/resume',json={'artifact_directory':token}).status_code,202)
                    self.assertEqual(settled(client,sid)['status'],'triage_failed');self.assertFalse(caller.calls)
            finally:sessions.close()
