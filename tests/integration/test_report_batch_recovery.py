"""Test omission recovery through the production graph, without live model calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from fastapi.testclient import TestClient
from projectclarity.reporting.report_batch_recovery import inspect_batch
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_batched_reporting import BatchCaller, many_facts
from tests.integration.test_research_graph import wait_for_completion


class OmissionCaller(BatchCaller):
    """Omit one fact once and add an uncited assertion to test quarantine."""

    def __call__(self, stage, request, sid):
        """Return incomplete initial work, then normal answers for targeted recovery.

        Args: stage: Task label; request: Real bounded request; sid: Session ID.
        Returns: Fixture JSON; only the first response omits a fact. Records the
            original request via the parent fake transport for routing assertions.
        """
        text = super().__call__(stage, request, sid)
        if len(self.calls) == 1:
            result = json.loads(text)
            missing = result['entries'].pop()['label_fact_ids'][0]
            result['summary'][0]['fact_ids'].remove(missing)
            result['summary'].append({'text': 'Unsupported claim must not be published.', 'fact_ids': []})
            return json.dumps(result)
        return text


class RecoveryTests(unittest.TestCase):
    """Keep the final evidence contract strict while salvaging valid work."""

    def test_graph_recovers_only_missing_fact_and_quarantines_uncited_text(self):
        """Exercise HTTP start, partial findings, targeted recovery and final merges.

        Args: None.
        Returns: None; verifies no duplicate assessment, exact fact accounting and
            retained audit text without publishing the unsupported assertion.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp); state = many_facts(root, 4); sid = state['session_id']
            caller = OmissionCaller(); sessions = Sessions(root, caller); sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    done = wait_for_completion(client, sid)
                    self.assertEqual(done['status'], 'report_completed', done.get('report'))
                    maps = [json.loads(req['messages'][0]['content']) for _, req, _ in caller.calls
                            if 'facts' in json.loads(req['messages'][0]['content'])]
                    self.assertEqual([len(p['facts']) for p in maps], [4, 1])
                    self.assertEqual(done['report']['facts_accounted'], 4)
                    output = client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertNotIn('Unsupported claim must not be published.', json.dumps(output))
                    folder = root / sid / done['report']['artifact_directory']
                    receipts = [json.loads(p.read_text()) for p in (folder/'tasks').glob('*.json')]
                    self.assertTrue(any(d.get('recovery', {}).get('withheld_uncited_summary') for d in receipts))
            finally:
                sessions.close()

    def test_retry_reuses_partial_response_after_recovery_request_fails(self):
        """Retry only failed recovery work while preserving the original raw response.

        Args: None.
        Returns: None; verifies the failed parent receipt is safely revalidated,
            the original four-fact assessment is not repeated, and all facts survive.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp); state = many_facts(root, 4); sid = state['session_id']
            caller = OmissionCaller(fail_at=2)
            sessions = Sessions(root, caller); sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    failed = wait_for_completion(client, sid)
                    self.assertEqual(failed['status'], 'report_failed')
                    client.post(f'/api/setup/{sid}/report/retry', json={
                        'previous_started_at': failed['report']['started_at']})
                    done = wait_for_completion(client, sid)
                    self.assertEqual(done['status'], 'report_completed', done.get('report'))
                    maps = [json.loads(req['messages'][0]['content']) for _, req, _ in caller.calls
                            if 'facts' in json.loads(req['messages'][0]['content'])]
                    self.assertEqual([len(p['facts']) for p in maps], [4, 1, 1])
                    self.assertEqual(done['report']['facts_accounted'], 4)
                    self.assertGreater(done['report']['reused_tasks'], 0)
            finally:
                sessions.close()

    def test_invalid_reference_is_not_repaired_or_hidden(self):
        """Reject invented citations even when another summary item is uncited.

        Args: None.
        Returns: None; confirms quarantine is not a bypass for reference checks.
        """
        result = {'entries': [], 'summary': [{'text': 'Claim', 'fact_ids': ['F999']},
                  {'text': 'Caveat', 'fact_ids': []}], 'gaps': [], 'exclusions': []}
        with self.assertRaises(ValueError):
            inspect_batch(json.dumps(result), {'facts': [{'id': 'F1'}], 'requirements': []})
