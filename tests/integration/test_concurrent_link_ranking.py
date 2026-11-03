"""Prove bounded title-ranking overlap and saved-batch reuse via HTTP."""
import hashlib
import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
import unittest
from fastapi.testclient import TestClient
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_link_ranking import ranking_fixture, RankingCaller
from tests.integration.test_research_graph import wait_for_completion


class ConcurrentLinkRankingTests(unittest.TestCase):
    """Exercise production scheduling, not a substitute parallel helper."""

    def exercise(self, cancel=False, changed=False):
        """Rank 300 anchors in twelve batches with six overlapping requests.

        Args:
            cancel: Stop the first wave through HTTP, then explicitly retry.
            changed: Alter one cached request to require a fresh model response.
        Returns:
            None; asserts overlap, reuse, source quotas, and immutable prior files.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=ranking_fixture(root);sid=state['session_id']
            state['link_ranking_concurrency']=6
            folder=root/sid/state['run']['artifact_directory']
            state['run']['sources']=[]
            for index in range(1,7):
                doc=folder/f'document-{index:03d}';doc.mkdir(exist_ok=True)
                body=(''.join(f'<a href="https://lead.example/{index}/{j}">Useful lead {j}</a>' for j in range(50))).encode()
                (doc/'source.bin').write_bytes(body)
                (doc/'web_acquisition.json').write_text(json.dumps({'content_type':'text/html',
                    'final_url':f'https://source.example/{index}','sha256':hashlib.sha256(body).hexdigest()}))
                state['run']['sources'].append({'id':index,'downloaded':True,'title':f'Source {index}'})
            gate=Barrier(6,timeout=5);lock=Lock();counts={'active':0,'peak':0,'started':0}
            class Caller(RankingCaller):
                """Hold the first six real ranking requests at a rendezvous."""
                def __call__(self,stage,request,session_id):
                    """Track overlap and optionally request cooperative stop.

                    Args: stage: Production label; request: Real prompt; session_id: Owner.
                    Returns: Fixture ratings or report, with counters always released.
                    """
                    if not stage.startswith('link-ranking-'):
                        return super().__call__(stage,request,session_id)
                    with lock:
                        counts['active']+=1;counts['started']+=1
                        number=counts['started'];counts['peak']=max(counts['peak'],counts['active'])
                    try:
                        if number<=6:
                            gate.wait()
                            if cancel:client.post(f'/api/setup/{sid}/stop')
                        with lock:return super().__call__(stage,request,session_id)
                    finally:
                        with lock:counts['active']-=1
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code,202)
                    done=wait_for_completion(client,sid)
                    self.assertEqual(counts['peak'],6)
                    progress=done['link_ranking']
                    self.assertEqual(progress['batches_finished'],6 if cancel else 12)
                    if cancel:
                        self.assertEqual(progress['status'],'cancelled')
                        self.assertNotIn('report',done)
                        old=root/sid/progress['artifact_directory']
                        if changed:
                            path=old/'batch-0001.json';receipt=json.loads(path.read_text())
                            receipt['request']['system']='Changed request'
                            path.write_text(json.dumps(receipt))
                        originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob('*') if p.is_file()}
                        self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code,202)
                        done=wait_for_completion(client,sid);progress=done['link_ranking']
                        self.assertEqual(progress['batches_reused'],5 if changed else 6)
                        self.assertEqual(progress['model_calls'],7 if changed else 6)
                        for path,digest in originals.items():
                            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
                    self.assertEqual(done['status'],'report_completed')
                    self.assertEqual(progress['candidates_ranked'],300)
                    self.assertEqual(counts['started'],13 if changed else 12)
                    data=sessions.link_ranking_data(sid)
                    self.assertEqual(len(data['selected']),10)
                    self.assertLessEqual(max(Counter(r['source_url'] for r in data['selected']).values()),3)
                    self.assertEqual(len({r['url'] for r in data['selected']}),10)
            finally:sessions.close()

    def test_six_overlapping_batches(self):
        """Run twelve batches through the graph.

        Args: None.
        Returns: None; requires six simultaneous requests and successful reporting.
        """
        self.exercise()

    def test_stop_retry_reuses_completed_batches(self):
        """Resume only unfinished work after a stop.

        Args: None.
        Returns: None; six saved batches survive and require no repeated calls.
        """
        self.exercise(cancel=True)

    def test_changed_request_is_rated_again(self):
        """Do not reuse a response from different model input.

        Args: None.
        Returns: None; one changed request is rerun, five valid batches reused.
        """
        self.exercise(cancel=True,changed=True)
