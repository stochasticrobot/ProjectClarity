"""Exercise bounded reporting and recovery with deterministic external replies.

The production graph, report construction, validators and artifact writers remain
real. Fake model/token-count responses prove limits and bookkeeping, not semantic
quality. Portable fixtures cover conflicts, retries, overflow and cancellation.
"""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from projectclarity.model_access.model_client import ModelCalls
from projectclarity.reporting.report_payloads import compact_packet, request_size, INPUT_TOKEN_LIMIT
from projectclarity.reporting.report_generation import ReportRun, validate_report
from projectclarity.reporting.report_merging import validate_merge
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_report_generation import report_fixture
from tests.integration.test_research_graph import wait_for_completion


def many_facts(root, count=85):
    """Expand investigated fixture evidence without changing the production pipeline.

    Args: root: Temporary sessions directory; count: Number of distinct source facts.
    Returns: Session with uniquely identified facts about one fixture company.
        Saved artifacts are local test data, never a copy of the user's session.
    """
    state = report_fixture(root)
    path = root / state['session_id'] / state['investigation']['artifact_directory'] / 'report_knowledge.json'
    store = json.loads(path.read_text())
    template = store['facts'][0]
    store['facts'] = []
    for i in range(count):
        fact = copy.deepcopy(template)
        fact['fact_id'] = f'original-{i}'
        fact['fact']['statement'] = f'Fixture company has property {i}.'
        store['facts'].append(fact)
    path.write_text(json.dumps(store))
    state['investigation']['report_knowledge'] = count
    state['link_ranking'] = {'status': 'completed'}
    return state


class BatchCaller:
    """Simulate token counts and structured responses without replacing report work."""
    model = 'offline-report-fixture'

    def __init__(self, fail_at=None, stop=None):
        """Configure a one-time failed generation or stop at the request boundary.

        Args: fail_at: One-based generation index to fail; stop: Optional Event.
        Returns: None; captures complete requests and counts for assertions.
        """
        self.calls, self.counts = [], []
        self.fail_at, self.stop = fail_at, stop

    def count_input_tokens(self, request):
        """Supply a conservative offline count rather than claim live token accuracy.

        Args: request: Complete production request including prompt/schema.
        Returns: UTF8 bytes plus overhead; records the exact counted request.
        """
        count = request_size(request) + 1024
        self.counts.append((copy.deepcopy(request), count))
        return count

    def __call__(self, stage, request, sid):
        """Produce complete findings or bounded merges for the real requested task.

        Args: stage: Production receipt label; request: Actual payload; sid: Session.
        Returns: Schema-shaped JSON; optional failure/cancellation does not retry.
        """
        self.calls.append((stage, copy.deepcopy(request), sid))
        assert request_size(request) + 1024 <= INPUT_TOKEN_LIMIT
        if self.stop:
            self.stop.set()
        if len(self.calls) == self.fail_at:
            raise RuntimeError('Fixture request failure.')
        data = json.loads(request['messages'][0]['content'])
        if 'facts' in data:
            entries = [{'label':'Fixture company', 'label_fact_ids':[fact['id']],
                        'fields':[{'requirement_id':r['id'], 'status':'supported',
                                   'value':'Fixture supported value', 'fact_ids':[fact['id']]}
                                  for r in data['requirements']]} for fact in data['facts']]
            return json.dumps({'entries':entries, 'summary':[{'text':'Fixture supported findings.',
                'fact_ids':[fact['id'] for fact in data['facts']]}], 'gaps':[], 'exclusions':[]})
        rows = data['findings']
        ids = [row['id'] for row in rows]
        if 'fields' not in rows[0]:
            return json.dumps({'summary':[{'text':'Combined fixture findings.', 'fact_ids':ids}]})
        fields = []
        for requirement in data['requirements']:
            prior = [next(f for f in row['fields'] if f['requirement_id']==requirement['id']) for row in rows]
            values = {f['value'] for f in prior if f['status']=='supported'}
            conflict = len(values)>1 or any(f['status']=='conflicting' for f in prior)
            fields.append({'requirement_id':requirement['id'], 'status':'conflicting' if conflict else 'supported',
                           'value':' / '.join(sorted(values)) or 'Conflicting fixture values',
                           'fact_ids':[row['id'] for row,field in zip(rows,prior) if field['has_evidence']]})
        return json.dumps({'entries':[{'input_ids':ids, 'entry':{'label':'Fixture company',
            'label_fact_ids':ids, 'fields':fields}}]})


class BatchedReportTests(unittest.TestCase):
    """Verify actual report routing, complete accounting and finite bounded work."""

    def test_http_batches_merges_and_accounts_for_every_fact(self):
        """Exercise multiple map batches, hierarchical entity merges and summary merge.

        Args: None.
        Returns: None; every generation is bounded and every fact survives in final
            citations. The final document is never sent as a generation request.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,180);sid=state['session_id']
            caller=BatchCaller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code,202)
                    result=wait_for_completion(client,sid)
                    self.assertEqual(result['status'],'report_completed',result.get('report'))
                    self.assertEqual(result['report']['facts_accounted'],180)
                    self.assertGreater(result['report']['batches_finished'],1)
                    self.assertGreater(result['report']['merge_tasks_finished'],1)
                    report=client.get(f'/api/setup/{sid}/report-data').json()
                    self.assertEqual(len(report['entries']),1)
                    self.assertEqual(len(report['entries'][0]['label_fact_ids']),180)
                    self.assertEqual(len(report['summary'][0]['fact_ids']),180)
                    self.assertLessEqual(result['report']['largest_counted_input'],INPUT_TOKEN_LIMIT)
                    self.assertTrue(all(request_size(request)+1024<=INPUT_TOKEN_LIMIT for _,request,_ in caller.calls))
            finally:
                sessions.close()

    def test_explicit_retry_reuses_validated_batches_only(self):
        """Retry a failed report without paying again for its successful map tasks.

        Args: None.
        Returns: None; old receipts stay unchanged, successful exact requests are
            not regenerated, and stale retry clicks do not add calls.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root);sid=state['session_id']
            caller=BatchCaller(fail_at=2);sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    failed=wait_for_completion(client,sid)
                    self.assertEqual(failed['status'],'report_failed')
                    sessions.executor.submit(lambda:None).result(timeout=5)
                    folder=root/sid/failed['report']['artifact_directory']
                    originals={path:path.read_bytes() for path in folder.rglob('*') if path.is_file()}
                    successful={json.dumps(row['request'],sort_keys=True) for path in (folder/'tasks').glob('*.json')
                                if (row:=json.loads(path.read_text()))['status']=='checks_pass'}
                    before=len(caller.calls)
                    payload={'previous_started_at':failed['report']['started_at']}
                    client.post(f'/api/setup/{sid}/report/retry',json=payload)
                    done=wait_for_completion(client,sid)
                    self.assertEqual(done['status'],'report_completed',done.get('report'))
                    self.assertGreater(done['report']['reused_tasks'],0)
                    self.assertTrue(all(json.dumps(request,sort_keys=True) not in successful for _,request,_ in caller.calls[before:]))
                    for path,raw in originals.items():self.assertEqual(path.read_bytes(),raw)
                    count=len(caller.calls);client.post(f'/api/setup/{sid}/report/retry',json=payload)
                    self.assertEqual(len(caller.calls),count)
            finally:
                sessions.close()

    def test_oversized_single_fact_and_cancellation_never_send_more(self):
        """Reject an indivisible oversized fact and stop between saved batches.

        Args: None.
        Returns: None; no over-limit generation or post-stop request is attempted.
        """
        for oversize in (True,False):
            with self.subTest(oversize=oversize),TemporaryDirectory() as tmp:
                root=Path(tmp);state=many_facts(root,1 if oversize else 85);sid=state['session_id']
                if oversize:
                    path=root/sid/state['investigation']['artifact_directory']/'report_knowledge.json'
                    data=json.loads(path.read_text());data['facts'][0]['fact']['statement']='x'*100000
                    path.write_text(json.dumps(data))
                stop=Event();caller=BatchCaller(stop=None if oversize else stop)
                worker=ReportRun(root/sid,state,lambda _:None,caller,stop);worker.execute()
                self.assertEqual(state['report']['status'],'failed' if oversize else 'cancelled')
                self.assertEqual(len(caller.calls),0 if oversize else 1)
                if oversize:self.assertIn('single facts record exceeds',state['report']['error'])

    def test_compaction_preserves_referenced_context_and_all_quotes(self):
        """Share identical evidence without dropping qualifications or entity scopes.

        Args: None.
        Returns: None; expansion restores complete fact/evidence data and referenced
            entity definitions. Unrelated extraction entities stay out of requests.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,3);sid=state['session_id']
            worker=ReportRun(root/sid,state,lambda _:None,BatchCaller(),Event())
            original=copy.deepcopy(worker.packet)
            packet=compact_packet(worker.packet,worker.packet['facts'])
            for old,new in zip(original['facts'],packet['facts']):
                self.assertEqual(new['fact'],old['fact'])
                self.assertEqual([packet['evidence_passages'][ref] for ref in new['evidence']],old['evidence'])
                wanted={old['fact'].get(k) for k in ('subject_id','object_id')}-{None}
                self.assertEqual([packet['entity_definitions'][ref] for ref in new['entities']],
                                 [entity for entity in old['entities'] if entity['id'] in wanted])
            self.assertEqual(worker.packet,original)

    def test_merge_rejects_lost_conflicts_and_missing_inputs(self):
        """Ensure merge output cannot silently promote conflicting evidence.

        Args: None.
        Returns: None; source disagreement and omitted findings fail validation.
        """
        entries=[{'label':'Acme','label_fact_ids':[f'F{i}'],'fields':[{'requirement_id':'R1',
                  'status':'supported','value':value,'fact_ids':[f'F{i}']}]} for i,value in enumerate(('Canada','USA'),1)]
        answer={'entries':[{'input_ids':['N1','N2'],'entry':{'label':'Acme','label_fact_ids':['N1','N2'],
                'fields':[{'requirement_id':'R1','status':'supported','value':'Canada','fact_ids':['N1','N2']}]}}]}
        with self.assertRaisesRegex(ValueError,'conflict'):
            validate_merge(json.dumps(answer),entries,[{'id':'R1'}],'entries')
        answer['entries'][0]['entry']['fields'][0].update(status='conflicting',value='Canada / USA')
        merged=validate_merge(json.dumps(answer),entries,[{'id':'R1'}],'entries')
        self.assertEqual(merged[0]['fields'][0]['fact_ids'],['F1','F2'])
        with self.assertRaises(ValueError):
            validate_merge('{"summary":[{"text":"Only one","fact_ids":["N1"]}]}',
                           [{'text':'A','fact_ids':['F1']},{'text':'B','fact_ids':['F2']}],[],'summary')

    def test_provider_counter_includes_schema_and_fails_closed(self):
        """Check token-count API arguments without a real provider request.

        Args: None.
        Returns: None; exact messages/system/schema reach counting, and a failed
            count never turns into a generation call or approximate fallback.
        """
        with TemporaryDirectory() as tmp,patch.dict('os.environ',{'ANTHROPIC_API_KEY':'fixture'}),patch('projectclarity.model_access.model_client.Anthropic') as provider:
            caller=ModelCalls(Path(tmp),Path(tmp)/'unused')
            api=provider.return_value.__enter__.return_value.messages
            api.count_tokens.return_value.input_tokens=123
            request={'model':'fixture','system':'instructions','messages':[],
                     'output_config':{'format':{'type':'json_schema','schema':{}}},'max_tokens':100,'timeout':180}
            self.assertEqual(caller.count_input_tokens(request),123)
            self.assertEqual(api.count_tokens.call_args.kwargs,{k:request[k] for k in ('model','system','messages','output_config')})
            api.count_tokens.side_effect=RuntimeError('failure')
            with self.assertRaises(RuntimeError):caller.count_input_tokens(request)
            api.create.assert_not_called()

    def test_count_failure_blocks_generation_and_response_checkpoint_is_reusable(self):
        """Fail closed on counting and recover a completed reply saved before validation.

        Args: None.
        Returns: None; count failure sends zero generations, and explicit replay
            revalidates the checkpoint instead of requesting that response again.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,2);sid=state['session_id']
            caller=BatchCaller()
            with patch.object(caller,'count_input_tokens',side_effect=RuntimeError('Count unavailable')):
                worker=ReportRun(root/sid,state,lambda _:None,caller,Event());worker.execute()
            self.assertEqual(state['report']['status'],'failed')
            self.assertEqual(caller.calls,[])
            worker=ReportRun(root/sid,state,lambda _:None,caller,Event());worker.execute()
            self.assertEqual(state['report']['status'],'completed')
            prior=copy.deepcopy(state['report'])
            task=next((worker.root/'tasks').glob('*.json'))
            receipt=json.loads(task.read_text());receipt['status']='response_received'
            receipt.pop('result_sha256',None);task.write_text(json.dumps(receipt))
            state['report_history']=[prior]
            calls=len(caller.calls)
            replay=ReportRun(root/sid,state,lambda _:None,caller,Event());replay.execute()
            self.assertEqual(state['report']['status'],'completed')
            self.assertEqual(len(caller.calls),calls)
            self.assertEqual(state['report']['reused_tasks'],calls)

    def test_changed_evidence_invalidates_reuse(self):
        """Never reuse an old batch merely because its fact IDs stayed the same.

        Args: None.
        Returns: None; changed source evidence requires fresh task generation.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=many_facts(root,2);sid=state['session_id'];caller=BatchCaller()
            worker=ReportRun(root/sid,state,lambda _:None,caller,Event());worker.execute()
            state['report_history']=[copy.deepcopy(state['report'])]
            path=root/sid/state['investigation']['artifact_directory']/'report_knowledge.json'
            data=json.loads(path.read_text());data['facts'][0]['fact']['statement']='Changed evidence statement.'
            path.write_text(json.dumps(data))
            before=len(caller.calls)
            replay=ReportRun(root/sid,state,lambda _:None,caller,Event());replay.execute()
            self.assertEqual(state['report']['status'],'completed')
            self.assertEqual(state['report']['reused_tasks'],0)
            self.assertGreater(len(caller.calls),before)
