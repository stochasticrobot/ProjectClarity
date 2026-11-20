"""Exercise the production search chain without contacting search or model services.

Controlled provider responses pass through the real search runner and document
collector. These checks prove fallback order, bounded attempts and saved provenance,
not live service availability or the relevance of provider results.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from projectclarity.documents.web_acquisition import SourceError
from projectclarity.search.public_search import search_public
from projectclarity.workflow.source_collection import SourceCollection
from tests.integration.test_document_pipeline import ExtractCaller


class Reader:
    """Supply a source page after discovery without network access."""
    def fetch(self, url):
        """Return a small readable document to the real parser.

        Args: url: Candidate address selected by the search chain.
        Returns: Standard acquisition tuple with fixture HTML and no warnings.
        """
        return url, {'content-type':'text/html'}, b'<html><body><p>Acme builds AI in Canada.</p></body></html>', []


class SearchProviderChainTests(unittest.TestCase):
    """Protect the actual default provider routing and public response parsing."""

    def test_production_order_and_each_stop_position(self):
        """Try each possible winning provider and complete exhaustion for five failure modes.

        Args: None.
        Returns: None; asserts the default production entry point advances on HTTP
            errors, reported errors and zero results, stops at the winner, preserves
            attempt receipts and sends discovered documents into extraction.
        """
        names=['duckduckgo','mwmbl','bing_rss','wikipedia']
        for failure in ('http','reported','empty','unusable','malformed'):
            for winner in range(5):
                with self.subTest(failure=failure,winner=winner), TemporaryDirectory() as tmp:
                    calls=[]
                    def provider(query, limit, cancelled, name='duckduckgo'):
                        """Return the selected outcome at the actual provider boundary.

                        Args: query, limit, cancelled: Search contract; name: provider ID.
                        Returns: Fixture receipt or HTTP-shaped SourceError, no network.
                        """
                        cancelled();calls.append(name)
                        if names.index(name)==winner:
                            return {'provider':name,'results':[{'url':'https://example.org/evidence','title':'Evidence','snippet':'Lead'}]}
                        if failure=='http':raise SourceError('search_error','HTTP 429',{'http_status':429})
                        if failure=='reported':return {'error':'Provider error','results':[]}
                        if failure=='unusable':return {'results':[{'url':'javascript:invalid','title':'Invalid','snippet':''}]}
                        if failure=='malformed':return {'unexpected':'envelope'}
                        return {'provider':name,'results':[]}
                    def wikipedia(query, limit, cancelled):
                        """Adapt the Wikipedia callable's three-argument signature.

                        Args: query, limit, cancelled: Original query and search controls.
                        Returns: Controlled provider outcome with Wikipedia identity.
                        """
                        return provider(query,limit,cancelled,'wikipedia')
                    state={'session_id':str(uuid4()),'research_question':'AI?',
                           'information_needed':[],'searches':[{'query':'Canada AI'}]}
                    with patch('projectclarity.search.web_search._search_once',side_effect=provider), \
                         patch('projectclarity.search.web_search.search_public',side_effect=provider), \
                         patch('projectclarity.search.web_search.search_wikipedia',side_effect=wikipedia), \
                         patch('projectclarity.search.web_search.SEARCH_INTERVAL_SECONDS',0), \
                         patch('projectclarity.search.web_search._search_finished',{}):
                        run=SourceCollection(Path(tmp),state,lambda s:None,ExtractCaller(),Event(),reader=Reader())
                        run.execute()
                    self.assertEqual(calls,names[:min(winner+1,4)])
                    record=run.run['searches'][0]
                    self.assertEqual(len(record['attempts']),len(calls))
                    self.assertEqual(len(list(run.root.glob('search-1-attempt-*.json'))),len(calls))
                    if winner<4:
                        self.assertEqual(record['provider'],names[winner])
                        self.assertEqual(record['status'],'completed')
                        self.assertGreater(run.run['model_calls'],0)
                    else:
                        self.assertEqual(record['status'],'failed')
                        self.assertEqual(run.run['model_calls'],0)

    def test_cancel_between_providers(self):
        """A stop after primary failure must prevent all fallback requests.

        Args: None.
        Returns: None; confirms cancellation at the real source-collection boundary.
        """
        with TemporaryDirectory() as tmp:
            stop=Event()
            def blocked(query, limit, cancelled, provider):
                """Request cancellation just as the primary provider fails.

                Args: query, limit, cancelled, provider: Ordinary provider inputs.
                Returns: Never; raises a controlled error after setting the stop event.
                """
                stop.set();raise SourceError('search_error','Blocked')
            state={'session_id':str(uuid4()),'research_question':'AI?',
                   'information_needed':[],'searches':[{'query':'Canada AI'}]}
            with patch('projectclarity.search.web_search._search_once',side_effect=blocked), \
                 patch('projectclarity.search.web_search.search_public') as fallback, \
                 patch('projectclarity.search.web_search._search_finished',{}):
                run=SourceCollection(Path(tmp),state,lambda s:None,ExtractCaller(),stop,reader=Reader())
                run.execute()
            fallback.assert_not_called()
            self.assertEqual(run.run['status'],'cancelled')

    def test_public_adapter_responses(self):
        """Normalize actual wire formats and reject error envelopes or malformed bodies.

        Args: None.
        Returns: None; verifies HTTP errors, provider errors, malformed responses,
            empty results, XML declarations and successful bounded JSON/RSS parsing.
        """
        cases=[
            ('mwmbl',200,b'{"results":[{"url":"https://example.org","title":"Example","content":"Text"}]}',1),
            ('mwmbl',200,b'{"results":[]}',0),
            ('mwmbl',200,b'{"error":"Failed","results":[]}',None),
            ('mwmbl',200,b'{"detail":"Unavailable"}',None),
            ('mwmbl',429,b'limited',None),
            ('mwmbl',200,b'bad json',None),
            ('bing_rss',200,b'<rss><channel><item><link>https://example.org</link><title>Example</title><description>Text</description></item></channel></rss>',1),
            ('bing_rss',200,b'<rss><channel/></rss>',0),
            ('bing_rss',200,b'<rss><channel/><error>Failed</error></rss>',None),
            ('bing_rss',503,b'unavailable',None),
            ('bing_rss',200,b'<html>challenge</html>',None),
            ('bing_rss',200,b'<!DOCTYPE rss><rss><channel/></rss>',None),
        ]
        for name,status,body,count in cases:
            with self.subTest(name=name,status=status,body=body):
                def transport(url, limit, cancelled):
                    """Supply an HTTP response without opening a network connection.

                    Args: url: Encoded query URL; limit: byte cap; cancelled: stop check.
                    Returns: Chosen status, empty headers and controlled response bytes.
                    """
                    self.assertIn('Canada+AI',url)
                    return status,{},body
                if count is None:
                    with self.assertRaises(SourceError):search_public('Canada AI',5,lambda:None,name,transport)
                else:
                    receipt=search_public('Canada AI',5,lambda:None,name,transport)
                    self.assertEqual(len(receipt['results']),count)
