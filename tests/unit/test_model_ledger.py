"""Check that AI requests and reported usage remain visible even when work fails.

The real request-sending code is exercised with an invented AI service response
in place of a network connection. The tests check successful, rejected, incomplete
and failed requests, including saved older results and unknown usage. This protects
the numbers shown in the Usage panel without using real credentials or paid calls.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
from projectclarity.model_access.model_ledger import checked_call, read_ledger, write_entry
from projectclarity.model_access.model_client import ModelCalls


def response(text='{}', stop='end_turn'):
    """Build a provider-shaped response with known nonoverlapping usage counters.

    Args: text: Response body; stop: Provider stop reason.
    Returns: Response stub with native usage, IDs and serializable provider fields.
    """
    data={'id':'msg-fixture','model':'fixture-model','usage':{'input_tokens':10,'output_tokens':5,'cache_read_input_tokens':7,'cache_creation_input_tokens':3},'stop_reason':stop}
    return SimpleNamespace(id=data['id'],model=data['model'],_request_id='req-fixture',stop_reason=stop,
        content=[SimpleNamespace(type='text',text=text)],model_dump=lambda **_:data)


class LedgerTests(unittest.TestCase):
    """Account for paid failures, unknown usage and validation independently."""
    def test_transport_validation_failure_and_cache_totals(self):
        """Use the real shared transport with external HTTP replaced at its boundary.

        Args: None.
        Returns: None; checks exact counts/cache totals and no receipt double count.
        """
        with TemporaryDirectory() as tmp,patch.dict('os.environ',{'ANTHROPIC_API_KEY':'fixture-secret'}),patch('projectclarity.model_access.model_client.Anthropic') as client:
            root=Path(tmp);sid='session';(root/sid).mkdir();caller=ModelCalls(root,root/'unused')
            client.return_value.__enter__.return_value.messages.create.side_effect=[response(),response('bad-json'),TimeoutError('fixture'),response('{}','max_tokens')]
            request={'model':'fixture-model','max_tokens':12,'system':'No secrets','messages':[]}
            self.assertEqual(checked_call(caller,'define',request,sid,json.loads),{})
            for stage in ['clarify','refine','search_queries']:
                with self.assertRaises(Exception):checked_call(caller,stage,request,sid,json.loads)
            data=read_ledger(root/sid)
            self.assertEqual(data['totals']['attempted'],4);self.assertEqual(data['totals']['unknown_usage'],1)
            self.assertEqual(data['totals']['known_tokens'],75)
            self.assertEqual(data['totals']['validated'],1);self.assertEqual(data['totals']['rejected'],1)
            self.assertEqual({r['outcome'] for r in data['calls']},{'response_received','failed','incomplete'})
            self.assertNotIn('fixture-secret',json.dumps(data))
            self.assertTrue(all(r['duration_seconds'] is not None for r in data['calls']))
            self.assertEqual(len(data['calls']),len(list((root/sid/'model-ledger').glob('*.json'))))
            self.assertEqual(client.call_args.kwargs['max_retries'],0)

    def test_not_sent_pending_and_legacy_unknown(self):
        """Missing credentials do not count as API attempts; old usage remains honest.

        Args: None.
        Returns: None; validates historical imports without rewrites and interrupted
            attempts with unknown usage rather than manufactured zero-token calls.
        """
        with TemporaryDirectory() as tmp,patch.dict('os.environ',{'ANTHROPIC_API_KEY':''}),patch('projectclarity.model_access.model_client.dotenv_values',return_value={}):
            root=Path(tmp);sid='session';folder=root/sid;folder.mkdir();caller=ModelCalls(root,root/'unused')
            with self.assertRaises(RuntimeError):caller('define',{'model':'fixture'},sid)
            self.assertEqual(read_ledger(folder)['totals']['attempted'],0)
            old=folder/'extract-doc1-chunk0-old.json';old.write_text(json.dumps({'request':{'model':'legacy'},'response':{'id':'old','usage':{'input_tokens':20,'output_tokens':4}}}));raw=old.read_bytes()
            legacy=read_ledger(folder);self.assertEqual(legacy['totals']['known_tokens'],24);self.assertEqual(legacy['totals']['attempted'],1)
            self.assertEqual(old.read_bytes(),raw)
            row={'call_id':'uncertain','stage':'report-x','stage_group':'Report','model':'fixture','started_at':'2026-01-01',
                 'outcome':'running','attempted':True,'validation':'unknown','usage':None,'receipt':'unknown.json'}
            write_entry(folder,row)
            data=read_ledger(folder,active=False)
            self.assertEqual(next(r for r in data['calls'] if r['call_id']=='uncertain')['outcome'],'interrupted')
            self.assertEqual(data['totals']['unknown_usage'],1)
            self.assertEqual(json.loads((folder/'model-ledger/uncertain.json').read_text())['outcome'],'running')
