"""Check that searches wait when required and preserve useful failure explanations.

Invented clocks and search responses make timing and failure cases repeatable.
The tests check the pause between searches, cancellation during waiting and the
information retained when a search fails. This protects the workflow from issuing
requests too quickly or hiding the cause of failure; no live searches are made.
"""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import projectclarity.search.web_search as web_search
from projectclarity.search.web_search import SourceError, search_web, search_worker


class SearchPacingTests(unittest.TestCase):
    """Verify pacing/cancellation and distinguish anti-bot pages from empty results."""

    def test_same_provider_spacing_and_cancellation(self):
        """Wait ten seconds between attempts, but never delay or send after cancellation.

        Args: None.
        Returns: None; checks dispatch times with a deterministic clock, not live waits.
        """
        now=[100.0];sent=[];waits=[]
        def sleep(seconds):
            """Advance only the fake clock.

            Args: seconds: Requested cooldown slice.
            Returns: None; mutates the test clock.
            """
            now[0]+=seconds
        def send(*args):
            """Record a single underlying provider request.

            Args: args: Ordinary search query, limit, cancellation callback and provider.
            Returns: Empty fixture receipt, without HTTP activity.
            """
            sent.append(now[0]);return {'results':[]}
        def cancelled():
            """Simulate a stop before work dispatch.

            Args: None.
            Returns: Never; raises InterruptedError.
            """
            raise InterruptedError('stopped')
        with patch.dict(web_search._search_finished,{},clear=True),patch('projectclarity.search.web_search.time.monotonic',side_effect=lambda:now[0]),patch('projectclarity.search.web_search.time.sleep',side_effect=sleep),patch('projectclarity.search.web_search._search_once',side_effect=send):
            search_web('first',5,lambda:None,on_wait=waits.append)
            search_web('second',5,lambda:None,on_wait=waits.append)
            self.assertGreaterEqual(sent[1]-sent[0],10)
            self.assertTrue(any(w>0 for w in waits))
            with self.assertRaises(InterruptedError):search_web('third',5,cancelled)
            self.assertEqual(len(sent),2)

    def test_challenge_marker_retained_in_worker_failure(self):
        """Capture a provider's HTTP202 challenge evidence without returning fake results.

        Args: None.
        Returns: None; verifies classified status, challenge marker and body fingerprint.
        """
        from ddgs.engines import ENGINES
        body=b'<form id="challenge-form">bots use duckduckgo</form>'
        response=SimpleNamespace(status_code=202,text=body.decode(),content=body)
        class Engine:
            """External engine fixture exercising the production request wrapper."""
            def __init__(self, timeout):
                """Install a non-network response adapter.

                Args: timeout: Ordinary worker deadline parameter.
                Returns: None; no network I/O.
                """
                self.http_client=SimpleNamespace(request=lambda *a,**kw:response)
            def search(self, query, region):
                """Call the real observed-request override in the worker subclass.

                Args: query: Search text; region: Requested region.
                Returns: No results only if the request wrapper incorrectly accepts a block.
                """
                self.request('POST','https://example.org');return []
        output=io.StringIO()
        with patch.dict(ENGINES['text'],{'duckduckgo':Engine}),patch('sys.stdin',io.StringIO('{"query":"example","limit":5}')),patch('sys.stdout',output):
            result=search_worker()
        receipt=json.loads(output.getvalue())
        self.assertEqual(result,1)
        self.assertEqual(receipt['code'],'search_blocked')
        self.assertEqual(receipt['http'][0]['status'],202)
        self.assertIn('challenge-form',receipt['http'][0]['challenge_markers'])
        self.assertEqual(len(receipt['http'][0]['body_sha256']),64)
        self.assertNotIn('results',receipt)

    def test_parent_preserves_failure_receipt(self):
        """Retain worker diagnostics across subprocess failure rather than just a string.

        Args: None.
        Returns: None; HTTP/challenge fields survive as SourceError.details.
        """
        from unittest.mock import MagicMock
        receipt={'provider':'duckduckgo','code':'search_blocked','error':'HTTP202','http':[{'status':202}]}
        process=MagicMock();process.communicate.return_value=(json.dumps(receipt),'');process.returncode=1;process.poll.return_value=1
        with patch('projectclarity.search.web_search.subprocess.Popen',return_value=process):
            with self.assertRaises(SourceError) as error:web_search._search_once('query',5,lambda:None)
        self.assertEqual(error.exception.code,'search_blocked')
        self.assertEqual(error.exception.details,receipt)
