"""Exercise concurrent report assessment through real HTTP and graph routing."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock, get_ident
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
from projectclarity.reporting.batched_reporting import BoundedReportBuilder
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_batched_reporting import many_facts, BatchCaller
from tests.integration.test_research_graph import wait_for_completion


class ConcurrentReportSelectionTests(unittest.TestCase):
    """Prove bounded execution and artifact reuse, not live model quality."""

    def exercise(self, cancel=False, recover=False, split=False, fail=False, limit=6):
        """Assess 120 facts in bounded groups with six barrier-held calls.

        Args:
            cancel: Stop first-wave calls through HTTP and explicitly retry.
            recover: Omit one fact per parent response to exercise targeted recovery.
            split: Simulate provider overflow for groups larger than ten facts.
            fail: Reject one response, retain siblings and retry explicitly.
            limit: Saved worker cap; one exercises serial execution.
        Returns:
            None; asserts actual overlap, no premature merges, complete accounting,
            single-thread receipt writes, immutable old artifacts and cached reuse.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,120);sid=state['session_id']
            state['report_selection_concurrency']=limit
            gate=Barrier(limit,timeout=5);lock=Lock()
            counts={'active':0,'peak':0,'started':0}
            writers=set();checkpoint=BoundedReportBuilder.checkpoint
            def record_checkpoint(builder,receipt):
                """Observe real receipt writes without replacing persistence.

                Args: builder: Current coordinator; receipt: Actual task result.
                Returns: Original checkpoint result; records writer thread identity.
                """
                writers.add(get_ident())
                return checkpoint(builder,receipt)
            class Caller(BatchCaller):
                """Use real report payloads with deterministic fake responses."""
                def count_input_tokens(self,request):
                    """Simulate exact preflight overflow without provider requests.

                    Args: request: Full production payload including schema.
                    Returns: Above-limit count only for designated oversized groups.
                    """
                    data=json.loads(request['messages'][0]['content'])
                    if split and len(data.get('facts',[]))>10:return 17000
                    return super().count_input_tokens(request)

                def __call__(self,stage,request,session_id):
                    """Hold first-wave calls and return complete or recoverable replies.

                    Args: stage: Unique label; request: Production input; session_id: Owner.
                    Returns: Fixture JSON; always releases overlap counters on failure.
                    """
                    data=json.loads(request['messages'][0]['content'])
                    if 'facts' not in data:
                        assert counts['active']==0, 'Merge overlapped evidence assessment'
                        return super().__call__(stage,request,session_id)
                    with lock:
                        counts['active']+=1;counts['started']+=1
                        number=counts['started'];counts['peak']=max(counts['peak'],counts['active'])
                    try:
                        if number<=limit:
                            gate.wait()
                            if cancel:client.post(f'/api/setup/{sid}/stop')
                        with lock:text=super().__call__(stage,request,session_id)
                        if fail and number==1:return '{"entries":[],"summary":[],"gaps":[],"exclusions":[]}'
                        if recover and len(data['facts'])>1:
                            result=json.loads(text);missing=data['facts'][-1]['id']
                            result['entries']=[e for e in result['entries'] if missing not in e['label_fact_ids']]
                            for part in result['summary']:
                                part['fact_ids']=[fid for fid in part['fact_ids'] if fid!=missing]
                            return json.dumps(result)
                        return text
                    finally:
                        with lock:counts['active']-=1
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with patch('projectclarity.reporting.batched_reporting.MAX_BATCH_FACTS',20 if split else 10), patch.object(BoundedReportBuilder,'checkpoint',record_checkpoint), TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code,202)
                    done=wait_for_completion(client,sid)
                    self.assertEqual(counts['peak'],limit)
                    self.assertEqual(len(writers),1)
                    if cancel or fail:
                        self.assertEqual(done['report']['status'],'cancelled' if cancel else 'failed')
                        self.assertTrue(all('-facts-' in stage for stage,_,_ in caller.calls))
                        if cancel:self.assertEqual(counts['started'],6)
                        sessions.executor.submit(lambda:None).result(timeout=5)
                        folder=root/sid/done['report']['artifact_directory']
                        originals={p:p.read_bytes() for p in folder.rglob('*') if p.is_file()}
                        successes={json.dumps(r['request'],sort_keys=True) for p in (folder/'tasks').glob('*.json') if (r:=json.loads(p.read_text()))['status']=='checks_pass'}
                        before=len(caller.calls)
                        self.assertEqual(client.post(f'/api/setup/{sid}/report/retry',json={'previous_started_at':done['report']['started_at']}).status_code,202)
                        done=wait_for_completion(client,sid)
                        self.assertGreaterEqual(done['report']['reused_tasks'],6 if cancel else 11)
                        for _,request,_ in caller.calls[before:]:
                            self.assertNotIn(json.dumps(request,sort_keys=True),successes)
                        for path,raw in originals.items():self.assertEqual(path.read_bytes(),raw)
                    self.assertEqual(done['status'],'report_completed',done.get('report'))
                    self.assertEqual(done['report']['facts_accounted'],120)
                    self.assertEqual(counts['started'],24 if recover else 13 if fail else 12)
                    self.assertEqual(done['report']['batches_finished'],24 if recover else 12)
                    self.assertLessEqual(done['report']['largest_counted_input'],16000)
                    return [request for stage,request,_ in caller.calls if '-facts-' not in stage]
            finally:sessions.close()

    def test_six_simultaneous_assessments(self):
        """Prove six concurrent calls and complete final accounting.

        Args: None.
        Returns: None; uses the real HTTP report entry point.
        """
        self.exercise()

    def test_stop_retry_reuses_first_wave(self):
        """Preserve first-wave replies without scheduling another wave after stop.

        Args: None.
        Returns: None; retry reuses saved results and finishes the report.
        """
        self.exercise(cancel=True)

    def test_recovery_uses_same_six_slots(self):
        """Keep missing-fact recovery inside its parent's worker slot.

        Args: None.
        Returns: None; all omissions are assessed without exceeding six calls.
        """
        self.exercise(recover=True)

    def test_overflow_splits_use_same_six_slots(self):
        """Split full requests before generation without spawning another pool.

        Args: None.
        Returns: None; every accepted generation respects the token ceiling.
        """
        self.exercise(split=True)

    def test_failure_retains_siblings_for_retry(self):
        """Block merging on an invalid batch but retain independent successes.

        Args: None.
        Returns: None; explicit retry reruns only the failed assessment.
        """
        self.exercise(fail=True)

    def test_single_slot_preserves_merge_input_order(self):
        """Compare serial and concurrent downstream requests, not completion order.

        Args: None.
        Returns: None; the same findings and citations reach every bounded merge.
        """
        self.assertEqual(self.exercise(limit=1), self.exercise())
