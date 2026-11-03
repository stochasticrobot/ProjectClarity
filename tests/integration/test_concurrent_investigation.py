"""Exercise concurrent investigation and resume through real HTTP/graph entry points."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
import unittest
from fastapi.testclient import TestClient
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_claim_investigation import investigation_fixture, InvestigationCaller, wait_terminal


class ConcurrentInvestigationTests(unittest.TestCase):
    """Prove bounded overlap and evidence-preserving resume, not model accuracy."""

    def exercise(self, cancel=False, corrupt=False):
        """Investigate twelve claims with a barrier-proven first wave of six.

        Args: cancel: Stop through HTTP then resume saved reviews; corrupt: Change
            one completed receipt's saved evidence before resume to reject reuse.
        Returns: None; verifies concurrency/counters, stable rows, six reused
            reviews, stale-click safety and unchanged old artifacts where applicable.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=investigation_fixture(root,True,count=12);sid=state['session_id']
            state['investigation_concurrency']=6
            gate=Barrier(6,timeout=5);lock=Lock();counts={'active':0,'peak':0,'started':0}
            class Caller(InvestigationCaller):
                """Simulate model latency and deterministic cited decisions only."""
                def __call__(self, stage, request, session_id):
                    """Hold the first six reviews concurrently and optionally stop.

                    Args: stage: Unique task label; request: Real evidence packet;
                        session_id: Owner ID.
                    Returns: Valid fixture response; counters always decrement.
                    """
                    if not stage.startswith('investigate-'):
                        return super().__call__(stage,request,session_id)
                    with lock:
                        counts['active']+=1;counts['started']+=1
                        number=counts['started'];counts['peak']=max(counts['peak'],counts['active'])
                    try:
                        if number<=6:
                            gate.wait()
                            if cancel:client.post(f'/api/setup/{sid}/stop')
                        with lock:
                            return super().__call__(stage,request,session_id)
                    finally:
                        with lock:counts['active']-=1
            caller=Caller(['accept','reject']);sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/investigation')
                    done=wait_terminal(client,sid)
                    self.assertEqual(counts['peak'],6)
                    progress=done['investigation'];count=6 if cancel else 12
                    self.assertEqual(progress['claims_finished'],count)
                    self.assertEqual(progress['model_calls'],count)
                    self.assertEqual(progress['status'],'cancelled' if cancel else 'completed')
                    folder=root/sid/progress['artifact_directory']
                    rows=json.loads((folder/'reviews.json').read_text())['records']
                    self.assertEqual([r['id'] for r in rows],list(range(1,13)))
                    self.assertEqual(progress['active_claims'],[])
                    if cancel:
                        old_files={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
                        token=progress['artifact_directory']
                        if corrupt:
                            path=folder/'claim-0001.json';receipt=json.loads(path.read_text())
                            receipt['request']['system']='Changed';path.write_text(json.dumps(receipt))
                        payload={'previous_artifact':token,'concurrency':6}
                        self.assertEqual(client.post(f'/api/setup/{sid}/investigation/resume',json=payload).status_code,202)
                        resumed=wait_terminal(client,sid)['investigation']
                        self.assertEqual(resumed['claims_reused'],5 if corrupt else 6)
                        self.assertEqual(resumed['model_calls'],6)
                        self.assertEqual(resumed['failed'],1 if corrupt else 0)
                        self.assertEqual(counts['started'],12)
                        before=len(caller.calls)
                        client.post(f'/api/setup/{sid}/investigation/resume',json=payload)
                        self.assertEqual(len(caller.calls),before)
                        if not corrupt:
                            for path,digest in old_files.items():
                                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            finally:sessions.close()

    def test_parallel_claims(self):
        """Reach six simultaneous reviews through the production graph.

        Args: None.
        Returns: None; preserves claim order and finishes downstream reporting.
        """
        self.exercise()

    def test_stop_resume_reuses_acceptances_and_rejections(self):
        """Drain first wave and reuse its reviews without repeated model requests.

        Args: None.
        Returns: None; saved attempts remain unchanged and duplicate clicks safe.
        """
        self.exercise(cancel=True)

    def test_changed_request_cannot_be_reused(self):
        """Reject changed cached evidence without pretending it is a valid review.

        Args: None.
        Returns: None; affected claim stays unreviewed, valid neighbors survive.
        """
        self.exercise(cancel=True,corrupt=True)
