"""Exercise ranking through the real HTTP workflow with saved HTML and fake replies.

No live model or network transport is used. These checks establish provenance,
quota enforcement, failure handling and report handoff, not semantic rating quality.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from fastapi.testclient import TestClient
from projectclarity.documents.document_links import document_links
from projectclarity.link_ranking.link_scoring import validate_ratings, select_links
from projectclarity.link_ranking.link_ranking_stage import LinkRankingStage
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_report_generation import report_fixture, ReportCaller
from tests.integration.test_research_graph import wait_for_completion


def ranking_fixture(root):
    """Add saved HTML links to real investigated evidence for offline routing tests.

    Args: root: Temporary sessions directory.
    Returns: Session with four source pages and two report-evidence facts. Each
        page has eight links, including one destination shared by all four pages.
    """
    state = report_fixture(root)
    source_root = root / state['session_id'] / state['run']['artifact_directory']
    state['run']['sources'] = []
    for index in range(1, 5):
        folder = source_root / f'document-{index:03d}'
        folder.mkdir(exist_ok=True)
        links = ''.join(f'<a href="https://leads.example/{index}/{j}">AI company {j}</a>' for j in range(7))
        body = (f'<title>Source {index}</title>' + links + '<a href="https://leads.example/shared#x">Shared lead</a>').encode()
        (folder / 'source.bin').write_bytes(body)
        (folder / 'web_acquisition.json').write_text(json.dumps({'content_type':'text/html',
            'final_url':f'https://source.example/{index}', 'sha256':hashlib.sha256(body).hexdigest()}))
        state['run']['sources'].append({'id':index, 'downloaded':True, 'title':f'Search title {index}'})
    return state


class RankingCaller(ReportCaller):
    """Return deterministic title ratings and reuse the real report test transport."""

    def __call__(self, stage, request, sid):
        """Rate each supplied occurrence without replacing ranking or graph logic.

        Args: stage: Actual stage label; request: Production payload; sid: Session ID.
        Returns: Complete offline JSON ratings or the fixture report response.
        """
        if stage.startswith('link-ranking-'):
            self.calls.append((stage, request, sid))
            links = json.loads(request['messages'][0]['content'])['links']
            return json.dumps({'ratings':[{'id':row['id'], 'rating':'high',
                'reason':'Title suggests useful AI information.'} for row in links]})
        return super().__call__(stage, request, sid)


class LinkRankingTests(unittest.TestCase):
    """Check the public workflow and score rules with no external work."""

    def test_http_ranks_before_report_preserves_sources_and_does_not_replay(self):
        """Run actual ranking batches and reporting through the historical action.

        Args: None.
        Returns: None; asserts exact contribution scores, deduplication, quotas,
            two title batches, unchanged input bytes and read-only page/API access.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp);state = ranking_fixture(root);sid = state['session_id']
            originals = {path:hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (root/sid).rglob('*') if path.is_file()}
            caller = RankingCaller();sessions = Sessions(root, caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post(f'/api/setup/{sid}/report').status_code, 202)
                    result = wait_for_completion(client, sid)
                    self.assertEqual(result['status'], 'report_completed')
                    data = client.get(f'/api/setup/{sid}/link-ranking-data').json()
                    self.assertEqual(len(data['candidates']), 29)
                    self.assertEqual(len(data['selected']), 10)
                    shared=next(row for row in data['candidates'] if row['url'].endswith('/shared'))
                    self.assertEqual(len(shared['source_associations']),4)
                    self.assertEqual(result['link_ranking']['duplicates_combined'],3)
                    self.assertEqual(len({row['url'] for row in data['selected']}), 10)
                    self.assertLessEqual(max(Counter(row['source_document_id'] for row in data['selected']).values()), 3)
                    evidence = json.loads((root/sid/state['investigation']['artifact_directory']/'report_knowledge.json').read_text())['facts']
                    counts = Counter(str(row['source']['document_id']) for row in evidence)
                    for row in data['candidates']:
                        self.assertEqual(row['document_score'], 100*counts[str(row['source_document_id'])]/len(evidence))
                        self.assertEqual(row['title_score'], 25)
                    self.assertEqual([stage.split('-')[0] for stage,_,_ in caller.calls], ['link','link','report'])
                    self.assertEqual(client.get(f'/links/{sid}').status_code, 200)
                    self.assertEqual(client.get('/assets/link_ranking.js').status_code, 200)
                    client.post(f'/api/setup/{sid}/report')
                    self.assertEqual(len(caller.calls), 3)
                    for path,digest in originals.items():
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            finally:
                sessions.close()

    def test_incomplete_batch_is_unranked_and_stops_before_report(self):
        """Reject missing IDs instead of assigning invented low scores.

        Args: None.
        Returns: None; all failed batches stop the real workflow before reporting.
        """
        class InvalidCaller(RankingCaller):
            """Deliberately omit all requested IDs to exercise production rejection."""
            def __call__(self, stage, request, sid):
                """Supply invalid ranking only; record any unexpected report call.

                Args: stage: Stage label; request: Actual payload; sid: Session ID.
                Returns: JSON with no ratings, which must fail exact coverage.
                """
                self.calls.append((stage, request, sid))
                return '{"ratings":[]}'
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=ranking_fixture(root);sid=state['session_id']
            caller=InvalidCaller();sessions=Sessions(root, caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    result=wait_for_completion(client,sid)
                    self.assertEqual(result['status'],'link_ranking_failed')
                    self.assertNotIn('report',result)
                    self.assertTrue(all(stage.startswith('link-ranking-') for stage,_,_ in caller.calls))
                    self.assertEqual(sessions.link_ranking_data(sid)['selected'],[])
            finally:
                sessions.close()

    def test_cancel_and_zero_facts(self):
        """Keep cancellation explicit and make a zero denominator score zero.

        Args: None.
        Returns: None; no division error, no model calls after pre-dispatch stop.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=ranking_fixture(root);sid=state['session_id']
            path=root/sid/state['investigation']['artifact_directory']/'report_knowledge.json'
            data=json.loads(path.read_text());data['facts']=[];path.write_text(json.dumps(data))
            stop=Event();caller=RankingCaller()
            worker=LinkRankingStage(root/sid,state,lambda _:None,caller,stop)
            worker.collect()
            self.assertTrue(all(row['document_score']==0 for row in worker.rows))
            stop.set();worker.execute()
            self.assertEqual(state['link_ranking']['status'],'cancelled')
            self.assertEqual(caller.calls,[])

    def test_link_metadata_and_validation(self):
        """Resolve relative links safely without inventing titles or accepting IDs.

        Args: None.
        Returns: None; verifies base URL, fragments, unsupported schemes and exact
            response coverage. This does not inspect live destinations.
        """
        title,links=document_links(b'<title>Actual title</title><base href="/folder/">'
            b'<a href="next#one">Useful</a><a href="next#two">Useful</a>'
            b'<a href="javascript:alert(1)">Bad</a><a href="mailto:x@y">Mail</a>'
            b'<a href="other"><img alt="Image title"></a><a href="blank"></a>',
            'text/html','https://example.org/start','Fallback')
        self.assertEqual(title,'Actual title')
        self.assertEqual(links,[{'url':'https://example.org/folder/next','link_title':'Useful'},
            {'url':'https://example.org/folder/other','link_title':'Image title'},
            {'url':'https://example.org/folder/blank','link_title':''}])
        with self.assertRaises(ValueError):
            validate_ratings('{"ratings":[]}',[{'id':'L1'}])
        self.assertEqual(select_links([]),[])

    def test_redirect_aliases_share_source_quota(self):
        """Prevent duplicate document IDs from evading the same-page limit.

        Args: None.
        Returns: None; two acquisition records for one final page yield only three
            selected links even though their document IDs differ.
        """
        rows=[{'id':f'L{i}', 'url':f'https://destination.example/{i}',
               'source_document_id':i%2, 'source_url':'https://source.example/final',
               'document_score':10, 'title_score':25, 'status':'ranked'} for i in range(8)]
        self.assertEqual(len(select_links(rows)),3)

    def test_partial_batch_continues_to_report_and_failed_links_stay_unranked(self):
        """Retain a successful batch when a later request fails validation.

        Args: None.
        Returns: None; actual graph reports partial ranking and proceeds to the
            report without fabricating scores for the four failed destinations.
        """
        class PartialCaller(RankingCaller):
            """Fail only the second title batch; keep the normal report fixture."""
            def __call__(self, stage, request, sid):
                """Return invalid IDs on the second batch without retrying it.

                Args: stage: Actual label; request: Production payload; sid: Session ID.
                Returns: Normal fixture response except one incomplete rating batch.
                """
                if stage.startswith('link-ranking-') and stage.endswith('-2'):
                    self.calls.append((stage,request,sid))
                    return '{"ratings":[]}'
                return super().__call__(stage,request,sid)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=ranking_fixture(root);sid=state['session_id']
            caller=PartialCaller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    result=wait_for_completion(client,sid)
                    self.assertEqual(result['status'],'report_completed')
                    self.assertEqual(result['link_ranking']['status'],'partial')
                    data=sessions.link_ranking_data(sid)
                    self.assertEqual(sum(row['status']=='failed' for row in data['candidates']),4)
                    self.assertTrue(all(row['status']=='ranked' for row in data['selected']))
                    self.assertEqual(len(caller.calls),3)
            finally:
                sessions.close()

    def test_thousands_of_links_make_at_most_twenty_title_calls(self):
        """Prove the shortlist bound through the real HTTP/graph entry point.

        Args: None.
        Returns: None; 3,639 discovered occurrences collapse to 3,618 destinations,
            of which 17 are excluded, 500 shortlisted and 3,101 not assessed.
            Exactly twenty title calls precede reporting; no source owns over 50.
        """
        with TemporaryDirectory() as tmp:
            root=Path(tmp);state=ranking_fixture(root);sid=state['session_id']
            folder=root/sid/state['run']['artifact_directory']
            state['run']['sources']=[]
            for index in range(1,13):
                doc=folder/f'document-{index:03d}';doc.mkdir(exist_ok=True)
                source=f'https://source.example/{index}'
                links=''.join(f'<a href="https://leads.example/{index}/{j}">Research {j}</a>' for j in range(300))
                # Repeated shared destination and already-read original/final aliases
                # test deduplication and exclusions before the request boundary.
                # Source 1 self-link is removed by the document reader, leaving 3,639.
                links+='<a href="https://leads.example/shared">Shared content</a>'
                links+='<a href="https://source.example/1">Already read</a>'
                links+=f'<a href="https://leads.example/{index}/privacy-policy">Privacy</a>'
                if index<=4:
                    links+=f'<a href="https://leads.example/{index}/logo.png">Logo</a>'
                body=(f'<title>Source {index}</title>'+links).encode()
                (doc/'source.bin').write_bytes(body)
                (doc/'web_acquisition.json').write_text(json.dumps({'content_type':'text/html',
                    'final_url':source,'sha256':hashlib.sha256(body).hexdigest()}))
                state['run']['sources'].append({'id':index,'url':source,'final_url':source,'downloaded':True})
            caller=RankingCaller();sessions=Sessions(root,caller);sessions.write(state)
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post(f'/api/setup/{sid}/report')
                    result=wait_for_completion(client,sid)
                    self.assertEqual(result['status'],'report_completed')
                    progress=result['link_ranking'];data=sessions.link_ranking_data(sid)
                    self.assertEqual(progress['discovered_occurrences'],3639)
                    self.assertEqual(progress['unique_destinations'],3618)
                    self.assertEqual(progress['excluded'],17)
                    self.assertEqual(progress['shortlisted'],500)
                    self.assertEqual(progress['not_assessed'],3101)
                    short=[row for row in data['candidates'] if row['shortlisted']]
                    self.assertLessEqual(max(Counter(row['source_url'] for row in short).values()),50)
                    calls=[request for stage,request,_ in caller.calls if stage.startswith('link-ranking-')]
                    self.assertEqual(len(calls),20)
                    sent=[row for request in calls for row in json.loads(request['messages'][0]['content'])['links']]
                    self.assertEqual(len(sent),500)
                    self.assertEqual(len({row['url'] for row in sent}),500)
                    self.assertEqual({row['id'] for row in sent},{row['id'] for row in short})
                    self.assertEqual(len(data['selected']),10)
                    for row in data['candidates']:
                        if not row['shortlisted']:
                            self.assertIsNone(row['rating'])
                            self.assertIsNone(row['title_score'])
            finally:
                sessions.close()
