"""Check how the application collects documents and extracts facts from them.

Invented search results and web responses make successful downloads, access
restrictions and failures repeatable without contacting websites. The tests check
that source text is preserved, progress reflects actual work and repeated actions
do not duplicate a run. Controlled AI replies exercise the path through the app;
they do not establish the quality of real search results or extracted facts.
"""
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from projectclarity.documents.web_acquisition import RobotsReader, SourceError, canonical_url, public_address, split_text, document_text
from projectclarity.workflow.source_collection import SourceCollection
from projectclarity.ux.web_app import Sessions, create_app
from tests.integration.test_research_setup import FakeCaller


def no_cancel():
    """Supply a non-cancelling callback for bounded offline transport tests.

    Args: None.
    Returns: None; no side effects.
    """
    return None


class ExtractCaller(FakeCaller):
    """Replace provider I/O only; reuse actual extraction request and validation code."""
    def __call__(self, stage, request, sid):
        """Return one grounded fact per extraction chunk or select its existing ID.

        Args:
            stage: Model stage identifier.
            request: Actual request containing complete source chunk and requirements.
            sid: Owning session ID.
        Returns:
            Schema-valid extraction/selection JSON; records unmodified requests.
        """
        if stage.startswith('report-'):
            self.calls.append((stage,request,sid))
            data=json.loads(request['messages'][0]['content'])
            if 'items' in request['output_config']['format']['schema']['properties']:
                return json.dumps({'items':[],'unused_fact_ids':[f['id'] for f in data['facts']]})
            return json.dumps({'summary':[],'entries':[],'gaps':['Fixture contains no complete answer.'],
                'exclusions':[{'fact_id':f['id'],'reason':'Fixture excluded.'} for f in data['facts']]})
        self.calls.append((stage,request,sid))
        if stage.startswith('triage-'):
            facts=json.loads(request['messages'][0]['content'])['facts']
            return json.dumps({'decisions':[{'fact_id':f['fact_id'],'bucket':'report_knowledge','reason':'Complete statement supported by its quote.'} for f in facts]})
        if stage.startswith('evidence-'):
            facts=json.loads(request['messages'][0]['content'])['facts']
            return json.dumps({'selected_facts':[{'fact_id':f['fact_id'],'reason':'Identifies AI activity in Canada.'} for f in facts]})
        text=json.loads(request['messages'][0]['content'])['document_chunk']
        return json.dumps({'entities':[{'id':'acme','name':'Acme','type':'organization','subtype':None}],
            'facts':[{'subject_id':'acme','predicate':'states','value':text,'object_id':None,'statement':text,'quote':text}]})


def fixture_search(query, limit, cancelled):
    """Return a fixed overlapping URL pool to verify deduplication without live search.

    Args:
        query: Unmodified prepared query.
        limit: Requested result cap.
        cancelled: Work-boundary cancellation callback.
    Returns:
        Two source leads in a search receipt; does not download or inspect documents.
    """
    cancelled()
    return {'provider':'offline','results':[{'url':'https://example.org/doc#section','title':'Allowed','snippet':'Not evidence'},
                                            {'url':'https://example.org/blocked','title':'Blocked','snippet':'Not evidence'}]}


def fixture_transport(url, limit, cancelled):
    """Serve robots rules and one text document without external network activity.

    Args:
        url: Requested robots/source URL.
        limit: Bounded response size requested by the production reader.
        cancelled: Stop callback.
    Returns:
        Fake HTTP status, headers and bytes. A forbidden document GET fails the test.
    """
    cancelled()
    if url.endswith('/robots.txt'):
        return 200,{'content-type':'text/plain'},b'User-agent: *\nDisallow: /blocked\n'
    if url.endswith('/doc'):
        return 200,{'content-type':'text/html'},b'<html><body><p>Acme builds AI tools in Canada.</p></body></html>'
    raise AssertionError('Unexpected/forbidden fetch: '+url)


class FixtureRun(SourceCollection):
    """Execute the real graph and robots reader with only outside I/O replaced."""
    def __init__(self, root, state, publish, caller, stop):
        """Wire deterministic external adapters into the ordinary runner.

        Args:
            root: Session artifact folder.
            state: Prepared session definition.
            publish: Actual status persistence callback.
            caller: Fake extraction provider.
            stop: Actual cancellation event.
        Returns:
            None; configures the real per-source acquisition and extraction nodes.
        """
        super().__init__(root,state,publish,caller,stop,search=fixture_search)
        self.documents.reader=RobotsReader(self.root,self.documents.cancelled,self.documents.event,fixture_transport)


class AcquisitionTests(unittest.TestCase):
    """Verify no-fetch robots outcomes and lossless chunk preparation."""
    def test_disallow_allow_and_specific_agent(self):
        """Apply named-agent, wildcard and longest-match rules rather than prefix guesses.

        Args: None.
        Returns: None; blocked URLs never reach document transport.
        """
        calls=[]
        def transport(url, limit, cancelled):
            """Record requests and supply a more-specific named-agent robots policy.

            Args: url, limit, cancelled: production transport arguments.
            Returns: Fake policy or source response; records requested URLs.
            """
            calls.append(url)
            if url.endswith('robots.txt'):
                return 200,{},b'User-agent: *\nDisallow: /\n\nUser-agent: ClarityResearchBot\nDisallow: /private*\nAllow: /private/public$\n'
            return 200,{},b'Allowed'
        with tempfile.TemporaryDirectory() as tmp:
            reader=RobotsReader(Path(tmp),no_cancel,lambda _:None,transport)
            with self.assertRaises(SourceError) as error:
                reader.fetch('https://example.org/private/hidden')
            self.assertEqual(error.exception.code,'robots_blocked')
            self.assertEqual(calls,['https://example.org/robots.txt'])
            reader.fetch('https://example.org/private/public')
            self.assertEqual(calls[-1],'https://example.org/private/public')

    def test_unavailable_robots_never_downloads(self):
        """Retry throttled/server-error robots checks, then defer without downloading.

        Args: None.
        Returns: None; verifies three attempts, no document GET, and reuse of the deferred outcome.
        """
        for status in (429,503):
            calls=[]
            def transport(url, limit, cancelled):
                """Return a configured robots failure for this offline test.

                Args: url, limit, cancelled: production transport arguments.
                Returns: Configured HTTP response while recording the URL.
                """
                calls.append(url);return status,{},b'Unavailable'
            with tempfile.TemporaryDirectory() as tmp, patch.object(RobotsReader,'retry_wait'):
                reader=RobotsReader(Path(tmp),no_cancel,lambda _:None,transport)
                for path in ('one','two'):
                    with self.assertRaises(SourceError):reader.fetch('https://example.org/'+path)
                self.assertEqual(calls,['https://example.org/robots.txt']*3)

    def test_redirect_checks_destination_robots(self):
        """Prevent an allowed origin from redirecting the crawler into a disallowed target.

        Args: None.
        Returns: None; target page is never requested after its robots denial.
        """
        calls=[]
        def transport(url, limit, cancelled):
            """Provide one allowed redirect and a denied second-origin policy.

            Args: url, limit, cancelled: production transport arguments.
            Returns: Fake HTTP hop; raises on any unexpected target-page request.
            """
            calls.append(url)
            if url=='https://one.example/robots.txt':return 404,{},b''
            if url=='https://one.example/start':return 302,{'location':'https://two.example/private'},b''
            if url=='https://two.example/robots.txt':return 200,{},b'User-agent: *\nDisallow: /private'
            raise AssertionError(url)
        with tempfile.TemporaryDirectory() as tmp:
            reader=RobotsReader(Path(tmp),no_cancel,lambda _:None,transport)
            with self.assertRaises(SourceError):reader.fetch('https://one.example/start')
            self.assertNotIn('https://two.example/private',calls)

    def test_private_dns_and_url_credentials_rejected(self):
        """Reject private DNS and credential-bearing/non-web URLs before connection.

        Args: None.
        Returns: None; explicit unsafe inputs raise rather than entering the fetch pool.
        """
        for url in ('file:///etc/passwd','https://user:pass@example.org','https://example.org:9999/'):
            with self.assertRaises(ValueError):canonical_url(url)
        with patch('projectclarity.documents.web_acquisition.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(SourceError):public_address('https://example.org/')
        self.assertEqual(canonical_url('https://EXAMPLE.org:443/a?x=1#frag'),'https://example.org/a?x=1')

    def test_long_crawl_delay_is_not_ignored(self):
        """Skip sites requesting a delay beyond this run's supported wait range.

        Args: None.
        Returns: None; no document GET is issued after the long-delay rule.
        """
        calls=[]
        def transport(url, limit, cancelled):
            """Return a 90-second crawl-delay policy without fetching a page.

            Args: url, limit, cancelled: production transport arguments.
            Returns: Fake robots bytes and recorded request URL.
            """
            calls.append(url);return 200,{},b'User-agent: *\nCrawl-delay: 90'
        with tempfile.TemporaryDirectory() as tmp:
            reader=RobotsReader(Path(tmp),no_cancel,lambda _:None,transport)
            with self.assertRaises(SourceError) as error:reader.fetch('https://example.org/doc')
            self.assertEqual(error.exception.code,'robots_delay')
            self.assertEqual(len(calls),1)

    def test_chunks_cover_complete_parsed_text(self):
        """Preserve complete text across chunks, including documents above one MB.

        Args: None.
        Returns: None; checks supported parsing and lossless chunking above the former cap.
        """
        text=('A paragraph with names and dates.\n'*200)+'x'*3500
        chunks=split_text(text)
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(c)<=3000 for c in chunks))
        self.assertEqual(document_text(b'<p>Facts</p><script>bad()</script>','text/html')[0],'Facts')
        for content_type, body in [('text/plain', b'x'*1_048_577),
                                   ('text/html', b'<p>'+b'x'*1_048_577+b'</p>')]:
            parsed, _ = document_text(body, content_type)
            self.assertEqual(parsed, 'x'*1_048_577)
            large_chunks = split_text(parsed)
            self.assertEqual(''.join(large_chunks), parsed)
            self.assertTrue(all(0 < len(c) <= 3000 for c in large_chunks))


class RunTests(unittest.TestCase):
    """Exercise the actual HTTP entry, LangGraph and persisted knowledge outputs."""
    def test_real_run_routes_and_exhausts_pool_without_block_bypass(self):
        """Connect saved queries through acquisition to extraction and monitor receipts.

        Args: None.
        Returns: None; checks dedup, robots failure, source provenance, progress and replay prevention.
        """
        with tempfile.TemporaryDirectory() as tmp:
            caller=ExtractCaller()
            sessions=Sessions(Path(tmp),caller,runner_factory=FixtureRun)
            sid=str(uuid4())
            sessions.write({'session_id':sid,'original_question':'Canadian AI?','research_question':'Canadian AI companies?',
                            'information_needed':[{'kind':'entity_type','name':'Company','description':'Identify companies.'}],
                            'searches':[{'query':'one','purpose':'direct'},{'query':'two','purpose':'different'}],'status':'queries_ready'})
            with TestClient(create_app(sessions)) as client:
                first=client.post(f'/api/setup/{sid}/run')
                self.assertEqual(first.status_code,202)
                self.assertEqual(client.get(f'/run/{sid}').status_code,200)
                client.post(f'/api/setup/{sid}/run')
                for _ in range(600):
                    state=client.get(f'/api/setup/{sid}').json()
                    if state['status'] not in ('run_running','selection_running','triage_running','investigation_running','link_ranking_running','report_running'):break
                    time.sleep(.01)
                self.assertEqual(state['status'],'report_completed')
                self.assertEqual(state['triage']['report_knowledge'],1)
                self.assertEqual(state['run']['status'],'partial')
                self.assertEqual(state['selection']['facts_selected'],1)
                self.assertEqual(len(state['run']['sources']),2)
                self.assertEqual([note['query_index'] for note in state['run']['sources'][0]['provenance']],[1,2])
                self.assertEqual(state['run']['sources'][1]['status'],'robots_blocked')
                self.assertEqual(state['run']['chunks_finished'],1)
                self.assertEqual(state['run']['facts'],1)
                self.assertEqual(len(caller.calls),4)
                payload=json.loads(caller.calls[0][1]['messages'][0]['content'])
                self.assertEqual(payload['requested_information'],state['information_needed'])
                self.assertNotIn('Not evidence',payload['document_chunk'])
                inspected=client.get(f'/api/setup/{sid}/knowledge/1').json()
                self.assertEqual(inspected['knowledge'][0]['facts'][0]['quote'],inspected['document_text'])
                self.assertEqual(client.get(f'/api/setup/{sid}/knowledge/99').status_code,404)
                self.assertEqual(client.post(f'/api/setup/{sid}/run').json()['status'],'report_completed')
                self.assertEqual(len(caller.calls),4)
            sessions.close()

    def test_cancel_before_search_creates_no_false_completion(self):
        """Cancel the real runner before external work and retain an explicit terminal state.

        Args: None.
        Returns: None; no search or extraction I/O occurs and no successful empty run is claimed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stop=Event();stop.set();caller=ExtractCaller();published=[]
            state={'session_id':str(uuid4()),'research_question':'Question','information_needed':[],
                   'searches':[{'query':'one'},{'query':'two'}]}
            runner=FixtureRun(Path(tmp),state,lambda s:published.append(s['status']) if 'status' in s else None,caller,stop)
            runner.execute()
            self.assertEqual(state['status'],'run_cancelled')
            self.assertEqual(caller.calls,[])


if __name__=='__main__':unittest.main()
