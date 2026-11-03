"""Prove triage overlap through HTTP and the production research graph."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
import unittest
from fastapi.testclient import TestClient
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_claim_triage import selected_fixture, settled, TriageCaller


class ConcurrentTriageTests(unittest.TestCase):
    """Verify bounded overlap without replacing triage or its persistence code."""

    def exercise(self, cancel=False, invalid=False):
        """Run twelve chunks with a six-call first-wave barrier and real handoffs.

        Args: cancel: Stop through the real API after six calls enter; invalid:
            Return missing decisions for one batch to test isolated rejection.
        Returns: None; asserts overlap/cap, exact accounting, deterministic saved
            order, immutable upstream data, and in-flight preservation on stop.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state,originals=selected_fixture(root,count=12)
            state['triage_concurrency']=6;sid=state['session_id']
            gate=Barrier(6,timeout=5);lock=Lock();metrics={'active':0,'peak':0,'calls':0}
            class Caller(TriageCaller):
                """Delay only external replies to demonstrate simultaneous requests."""
                def __call__(self, stage, request, session_id):
                    """Track active triages and optionally stop or reject a batch.

                    Args: stage: Unique request label; request: Real payload;
                        session_id: Owning session.
                    Returns: Fake triage or downstream response. Active counts
                        are decremented on every exit, including failed assertions.
                    """
                    if not stage.startswith('triage-'):
                        return super().__call__(stage,request,session_id)
                    with lock:
                        metrics['active']+=1;metrics['calls']+=1
                        number=metrics['calls'];metrics['peak']=max(metrics['peak'],metrics['active'])
                    try:
                        if number<=6:
                            gate.wait()
                            if cancel:
                                response=client.post(f'/api/setup/{sid}/stop')
                                assert response.status_code==200
                        with lock:
                            text=super().__call__(stage,request,session_id)
                        if invalid and stage.endswith('batch1'):
                            return '{"decisions":[]}'
                        return text
                    finally:
                        with lock:
                            metrics['active']-=1
            caller=Caller(['report_knowledge']);sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    response=client.post(f'/api/setup/{sid}/triage')
                    self.assertEqual(response.status_code,202)
                    done=settled(client,sid);triage=done['triage']
                    self.assertEqual(metrics['peak'],6)
                    count=6 if cancel else 12
                    self.assertEqual(metrics['calls'],count)
                    self.assertEqual(triage['model_calls'],count)
                    self.assertEqual(triage['facts_reviewed'],count-int(invalid))
                    self.assertEqual(triage['status'],'cancelled' if cancel else 'partial' if invalid else 'completed')
                    folder=root/sid/triage['artifact_directory']
                    data=json.loads((folder/'decisions.json').read_text())
                    ids=[row['source']['document_id'] for row in data['facts']]
                    self.assertEqual(ids,sorted(set(ids)))
                    self.assertEqual(len(ids),count-int(invalid))
                    self.assertEqual(len(list(folder.glob('batch-*.json'))),count)
                    for path,digest in originals.items():
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
                    if cancel:
                        self.assertFalse(any(stage.startswith('investigate-') for stage,_,_ in caller.calls))
            finally:
                sessions.close()

    def test_six_calls_and_isolated_invalid_batch(self):
        """Reject one invalid response without losing valid parallel neighbors.

        Args: None.
        Returns: None; real graph hands off ordered accepted facts to investigation.
        """
        self.exercise(invalid=True)

    def test_stop_drains_six_without_starting_next_wave(self):
        """Stop actual concurrent HTTP work and retain each completed first-wave batch.

        Args: None.
        Returns: None; unstarted second-wave chunks remain unreviewed.
        """
        self.exercise(cancel=True)
