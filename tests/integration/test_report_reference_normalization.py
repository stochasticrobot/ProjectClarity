"""Check reference formatting across batch coverage, merging and explicit retry."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
from projectclarity.reporting.report_reference_normalization import normalize_references
from projectclarity.reporting.report_batch_recovery import inspect_batch
from projectclarity.reporting.report_merging import validate_merge
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_batched_reporting import many_facts, BatchCaller
from tests.integration.test_research_graph import wait_for_completion


class LowercaseCaller(BatchCaller):
    """Supply harmless case/outer-space variations, including merge references."""
    def __call__(self,stage,request,sid):
        """Vary reference formatting only, leaving report meaning unchanged.

        Args: stage: Actual call label; request: Production payload; sid: Owner ID.
        Returns: Otherwise valid fixture JSON with lowercased, padded references.
        """
        result=json.loads(super().__call__(stage,request,sid))
        def change(value):
            """Walk structured reference fields without rewriting prose.

            Args: value: Fixture dictionary/list.
            Returns: None; modifies only this fixture response's ID strings.
            """
            if isinstance(value,list):
                for item in value:change(item)
            elif isinstance(value,dict):
                for key,item in value.items():
                    if key in ('fact_ids','label_fact_ids','input_ids'):
                        value[key]=[' '+s.lower()+' ' for s in item]
                    elif key in ('fact_id','requirement_id'):value[key]=' '+item.lower()+' '
                    else:change(item)
        change(result)
        return json.dumps(result)


class ReportReferenceTests(unittest.TestCase):
    """Keep tolerant parsing distinct from invented evidence and identity guesses."""
    def test_ambiguous_unknown_and_prose(self):
        """Resolve only unique supplied IDs without altering caller data.

        Args: None.
        Returns: None; exact matches win and ambiguous/unknown variants fail.
        """
        source={'summary':[{'text':' f1 is literal prose ','fact_ids':[' f1 ']}]}
        result=normalize_references(source,['F1'],[])
        self.assertEqual(result['summary'][0]['fact_ids'],['F1'])
        self.assertEqual(result['summary'][0]['text'],' f1 is literal prose ')
        self.assertEqual(source['summary'][0]['fact_ids'],[' f1 '])
        for ids in (['F2'],['F1','f1']):
            with self.assertRaises(ValueError):normalize_references(source,ids,[])
        self.assertEqual(normalize_references({'fact_id':'F1'},['F1','f1'],[])['fact_id'],'F1')

    def test_normalize_before_coverage_and_exclusion_overlap(self):
        """Avoid false omissions when only the malformed ID cites the fact.

        Args: None.
        Returns: None; summary and exclusions count canonically with no repair.
        """
        packet={'requirements':[],'facts':[{'id':'F1'},{'id':'F2'}]}
        reply={'summary':[{'text':'Supported scope explanation','fact_ids':[' f1 ']}],
               'entries':[],'gaps':[],'exclusions':[{'fact_id':'f1','reason':'Scope'},{'fact_id':' f2 ','reason':'Unrelated'}]}
        result=inspect_batch(json.dumps(reply),packet)
        self.assertEqual(result['_recovery']['missing_fact_ids'],[])
        self.assertEqual(result['exclusions'],[{'fact_id':'F2','reason':'Unrelated'}])

    def test_summary_merge_and_unknowns(self):
        """Normalize local finding IDs without accepting missing ownership.

        Args: None.
        Returns: None; canonical original citations survive the merge.
        """
        items=[{'text':'One','fact_ids':['F1']},{'text':'Two','fact_ids':['F2']}]
        result=validate_merge('{"summary":[{"text":"Both","fact_ids":[" n1 ","n2"]}]}',items,[],'summary')
        self.assertEqual(result[0]['fact_ids'],['F1','F2'])
        with self.assertRaises(ValueError):
            validate_merge('{"summary":[{"text":"Both","fact_ids":["n3"]}]}',items,[],'summary')

    def test_report_page_retry_reuses_saved_raw_response_only(self):
        """Retry via production HTTP after simulating the former strict validator.

        Args: None.
        Returns: None; saved map reply is revalidated without a new map request,
            only report merges execute, old files persist and duplicate clicks do not replay.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,4);sid=state['session_id']
            caller=LowercaseCaller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    with patch('projectclarity.reporting.report_reference_normalization.normalize_references',side_effect=lambda result,*args:result):
                        client.post(f'/api/setup/{sid}/report')
                        failed=wait_for_completion(client,sid)
                    self.assertEqual(failed['status'],'report_failed')
                    sessions.executor.submit(lambda:None).result(timeout=5)
                    old=root/sid/failed['report']['artifact_directory']
                    originals={p:p.read_bytes() for p in old.rglob('*') if p.is_file()}
                    # Legacy report attempts may have no link-ranking metadata.
                    failed.pop('link_ranking');sessions.write(failed)
                    before=len(caller.calls)
                    token={'previous_started_at':failed['report']['started_at']}
                    self.assertEqual(client.post(f'/api/setup/{sid}/report/retry',json=token).status_code,202)
                    done=wait_for_completion(client,sid)
                    self.assertEqual(done['status'],'report_completed',done.get('report'))
                    self.assertEqual(done['report']['reused_tasks'],1)
                    self.assertEqual(done['report']['facts_accounted'],4)
                    self.assertTrue(all(stage.startswith('report-') and '-facts-' not in stage for stage,_,_ in caller.calls[before:]))
                    self.assertNotIn('link_ranking',done)
                    for p,raw in originals.items():self.assertEqual(p.read_bytes(),raw)
                    count=len(caller.calls);client.post(f'/api/setup/{sid}/report/retry',json=token)
                    self.assertEqual(len(caller.calls),count)
            finally:sessions.close()
