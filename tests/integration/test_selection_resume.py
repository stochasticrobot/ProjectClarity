"""Exercise explicit selection resume through real HTTP and production graph."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from fastapi.testclient import TestClient
from projectclarity.evidence_selection.evidence_selection_stage import EvidenceSelectionStage
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_evidence_selection_api import seed, settle, SelectorCaller


class ResumeTests(unittest.TestCase):
    """Reuse validated work without allowing stale clicks or changed-input reuse."""

    def test_resume_reuses_complete_batch_and_preserves_history(self):
        """Stop after one batch, then resume the remainder with six slots.

        Args: None.
        Returns: None; asserts one reused batch, only eleven new selection calls,
            unchanged prior artifacts and upstream bytes, and idempotent resubmit.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state,originals=seed(root,count=12);sid=state['session_id']
            stop=Event();first=SelectorCaller(stop=stop)
            old=EvidenceSelectionStage(root/sid,state,lambda _:None,first,stop);old.execute()
            self.assertEqual(state['selection']['batches_passed'],1)
            token=state['selection']['artifact_directory']
            originals.update({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in old.root.rglob('*') if p.is_file()})
            caller=SelectorCaller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    payload={'previous_artifact':token,'concurrency':6}
                    self.assertEqual(client.post(f'/api/setup/{sid}/selection/resume',json=payload).status_code,202)
                    done=settle(client,sid)
                    self.assertEqual(done['selection']['status'],'completed',done)
                    self.assertEqual(done['selection']['batches_reused'],1)
                    self.assertEqual(done['selection']['model_calls'],11)
                    self.assertEqual(done['selection']['concurrency'],6)
                    self.assertEqual(len([x for x in caller.calls if x[0].startswith('evidence-')]),11)
                    count=len(caller.calls)
                    client.post(f'/api/setup/{sid}/selection/resume',json=payload)
                    self.assertEqual(len(caller.calls),count)
                    for path,digest in originals.items():
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
            finally:
                sessions.close()
