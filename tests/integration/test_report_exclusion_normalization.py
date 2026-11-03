"""Check topic-independent used/excluded normalization and raw-response retention."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from fastapi.testclient import TestClient
from projectclarity.reporting.report_generation import validate_report
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_batched_reporting import BatchCaller, many_facts
from tests.integration.test_research_graph import wait_for_completion


class ReportExclusionNormalizationTests(unittest.TestCase):
    """Accept cited exclusion explanations without relaxing evidence checks."""

    def test_topic_independent_and_invalid_references_still_fail(self):
        """Normalize by fact ID while preserving other accounting safeguards.

        Args: None.
        Returns: None; arbitrary topics pass, invalid/missing/duplicate references
            and malformed exclusions fail, and caller-owned input stays unchanged.
        """
        packet={'requirements':[{'id':'R1'}],'facts':[{'id':'F1'},{'id':'F2'}]}
        for topic in ('Canadian startups','Forest biodiversity','Ancient poetry'):
            result={'summary':[{'text':topic+' scope explanation','fact_ids':['F1']}],
                    'entries':[],'gaps':[],'exclusions':[
                        {'fact_id':'F1','reason':'Outside answer scope'},
                        {'fact_id':'F2','reason':'Unrelated evidence'}]}
            raw=json.dumps(result)
            checked=validate_report(raw,packet)
            self.assertEqual(checked['exclusions'],[result['exclusions'][1]])
            self.assertEqual(checked['summary'],result['summary'])
            self.assertEqual(json.dumps(result),raw)
            for ids in (['unknown'], ['F1','F1'], []):
                invalid=copy.deepcopy(result);invalid['summary'][0]['fact_ids']=ids
                with self.assertRaises(ValueError):validate_report(json.dumps(invalid),packet)
            for exclusions in ([result['exclusions'][0]],result['exclusions']+[result['exclusions'][0]],
                               [{'fact_id':'F1','reason':''},result['exclusions'][1]]):
                invalid=copy.deepcopy(result);invalid['exclusions']=exclusions
                with self.assertRaises(ValueError):validate_report(json.dumps(invalid),packet)

    def test_http_report_keeps_raw_reply_and_publishes_normalized_output(self):
        """Exercise production report generation with cited/excluded overlap.

        Args: None.
        Returns: None; one batch succeeds without a repair call; raw receipt keeps
            the exclusion while the published report removes it and retains citations.
        """
        class Caller(BatchCaller):
            """Add one redundant exclusion to an otherwise valid model fixture."""
            def __call__(self,stage,request,sid):
                """Return a cited fact also marked excluded.

                Args: stage: Production label; request: Actual input; sid: Session ID.
                Returns: JSON with deliberate overlap, using no external provider.
                """
                result=json.loads(super().__call__(stage,request,sid))
                data=json.loads(request['messages'][0]['content'])
                if 'facts' in data:
                    result['exclusions'].append({'fact_id':data['facts'][0]['id'],'reason':'Outside answer scope'})
                return json.dumps(result)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,4);sid=state['session_id']
            caller=Caller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    done=wait_for_completion(client,sid)
                    self.assertEqual(done['status'],'report_completed',done.get('report'))
                    self.assertEqual(done['report']['facts_accounted'],4)
                    folder=root/sid/done['report']['artifact_directory']
                    report=json.loads((folder/'report.json').read_text())
                    self.assertEqual(report['exclusions'],[])
                    receipts=[json.loads(p.read_text()) for p in (folder/'tasks').glob('*.json')]
                    facts=[r for r in receipts if r['kind']=='facts']
                    self.assertEqual(len(facts),1)
                    self.assertEqual(len(json.loads(facts[0]['raw_text'])['exclusions']),1)
                    self.assertEqual(facts[0]['status'],'checks_pass')
            finally:sessions.close()
