"""Check how website access instructions affect whether a document can be downloaded.

Websites may provide robots.txt, a file describing which pages automated readers
may access. These tests supply different replies when that file is requested and
exercise the actual document-fetching code. They check that unavailable rules,
temporary failures and explicit restrictions are handled differently, without
contacting real websites.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from projectclarity.documents.web_acquisition import RobotsReader, SourceError


class PolicyTests(unittest.TestCase):
    """Absent policies permit access; explicit denies and unreachable policies do not."""
    def exercise(self,responses,allowed=True):
        """Run the real reader with only HTTP and wait time replaced.

        Args: responses: Robots statuses/headers/bodies or exceptions; allowed:
            Whether document acquisition should succeed.
        Returns: Requested URLs, retry delays and persisted robots receipt.
        """
        calls=[];remaining=iter(responses)
        def transport(url,limit,cancelled):
            """Provide one response per robots attempt, and content for allowed pages.

            Args: url, limit, cancelled: Actual acquisition transport arguments.
            Returns: HTTP tuple, or raises a supplied network error.
            """
            calls.append(url)
            if url.endswith('/robots.txt'):
                result=next(remaining)
                if isinstance(result,Exception):raise result
                return result
            return 200,{'content-type':'text/plain'},b'Actual document'
        with TemporaryDirectory() as tmp,patch.object(RobotsReader,'retry_wait') as wait,patch('projectclarity.documents.web_acquisition.time.sleep'):
            reader=RobotsReader(Path(tmp),lambda:None,lambda _:None,transport)
            reader.last={}
            # Disable ordinary per-document pacing only, not robots policy decisions.
            with patch('projectclarity.documents.web_acquisition.time.monotonic',side_effect=range(1000,2000)):
                if allowed:self.assertEqual(reader.fetch('https://example.org/doc')[2],b'Actual document')
                else:
                    with self.assertRaises(SourceError):reader.fetch('https://example.org/doc')
            receipt=json.loads(next(Path(tmp).glob('robots-*.json')).read_text())
            return calls,[c.args[0] for c in wait.call_args_list],receipt

    def test_missing_and_successful_statuses(self):
        """Accept unavailable 4xx and successful empty policies without false denial.

        Args: None.
        Returns: None; all cases reach document web_acquisition.
        """
        for status in [400,401,403,404,410,204]:
            calls,delays,receipt=self.exercise([(status,{},b'')]);self.assertEqual(len(calls),2);self.assertFalse(delays)
        calls,delays,receipt=self.exercise([(202,{},b'')]*3)
        self.assertEqual(len(calls),4);self.assertEqual(delays,[2,4]);self.assertEqual(receipt['decision'],'parsed_success')

    def test_retry_recovery_rules_and_persistent_errors(self):
        """Never bypass retrieved rules; retry transient failures only within bounds.

        Args: None.
        Returns: None; verifies network recovery, deny rules and failure exhaustion.
        """
        for first in [TimeoutError(),(503,{},b''),(429,{'retry-after':'7'},b'')]:
            calls,delays,_=self.exercise([first,(200,{},b'User-agent: *\nAllow: /')]);self.assertEqual(len(calls),3);self.assertEqual(len(delays),1)
        deny=b'User-agent: *\nDisallow: /'
        for replies in [[(200,{},deny)],[(202,{},deny)]*3,[(200,{},b'<html>\n'+deny+b'\n</html>')]]:
            calls,_,_=self.exercise(replies,False);self.assertNotIn('https://example.org/doc',calls)
        for replies in [[TimeoutError()]*3,[(503,{},b'')]*3,[(429,{},b'')]*3]:
            calls,delays,receipt=self.exercise(replies,False);self.assertEqual(len(calls),3);self.assertEqual(receipt['decision'],'deferred')
        calls,delays,_=self.exercise([(429,{'retry-after':'120'},b'')],False);self.assertEqual(len(calls),1);self.assertFalse(delays)

    def test_cancel_retry(self):
        """Cancellation during backoff prevents further network work.

        Args: None.
        Returns: None; cancellation is propagated, never cached as permission.
        """
        with TemporaryDirectory() as tmp:
            reader=RobotsReader(Path(tmp),lambda:None,lambda _:None,lambda *args:(503,{},b''))
            with patch.object(reader,'retry_wait',side_effect=InterruptedError):
                with self.assertRaises(InterruptedError):reader.policy('https://example.org/doc')
            self.assertEqual(reader.policies,{})
