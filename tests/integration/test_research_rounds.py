"""Exercise bounded linked-document rounds through the real HTTP workflow.

Only search, HTTP transport and model replies are replaced. Graph routing,
acquisition, extraction, review, ranking and cumulative reporting remain real.
These checks establish orchestration and provenance, not real-world answer quality.
"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from projectclarity.documents.web_acquisition import RobotsReader
from projectclarity.workflow.source_collection import SourceCollection
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_research_graph import FullWorkflowCaller, wait_for_completion
from tests.integration.test_item_reporting import contribution


class RoundCaller(FullWorkflowCaller):
    """Return grounded fixture answers, with optional report failure or extraction stop."""

    def __init__(self):
        """Prepare request capture and optional fault injection.

        Args: None.
        Returns: None; no external calls or routing decisions.
        """
        super().__init__(questions=[])
        self.fail_report = False
        self.exclude_second = False
        self.stop_on_second = None

    def __call__(self, stage, request, sid):
        """Answer actual production requests and optionally interrupt known boundaries.

        Args: stage: Actual call label; request: Full payload; sid: Session UUID.
        Returns: Schema-shaped fixture JSON. A requested report failure raises;
            an extraction callback can request a cooperative stop after receipt.
        """
        if stage in ('define', 'clarify', 'refine', 'search_queries'):
            return super().__call__(stage, request, sid)
        data = json.loads(request['messages'][0]['content'])
        if self.exclude_second and stage.startswith('evidence-') and 'page 2' in str(data):
            self.calls.append((stage, request, sid))
            return json.dumps({'selected_facts': []})
        if stage.startswith('link-ranking-'):
            self.calls.append((stage, request, sid))
            return json.dumps({'ratings': [{'id': row['id'], 'rating': 'high',
                'reason': 'Promising fixture link.'} for row in data['links']]})
        if stage.startswith('report-'):
            self.calls.append((stage, request, sid))
            if self.fail_report:
                raise RuntimeError('Deliberate offline report failure')
            return json.dumps({'items': [contribution('Acme', row['id'], row['fact']['statement'])
                for row in data['facts']], 'unused_fact_ids': []})
        result = super().__call__(stage, request, sid)
        if self.stop_on_second and 'page 2' in data.get('document_chunk', ''):
            self.stop_on_second()
        return result


class RoundRun(SourceCollection):
    """Serve a chain of pages plus backward links through the real robots reader."""
    searches_seen = []
    fetched = []
    page_limit = 9

    def __init__(self, root, state, publish, caller, stop):
        """Inject only external I/O, including for subsequent round constructors.

        Args: root, state, publish, caller, stop: Ordinary SourceCollection inputs.
        Returns: None; initializes the production collection and reader unchanged.
        """
        super().__init__(root, state, publish, caller, stop, search=self.search_fixture)
        self.documents.reader = RobotsReader(self.root, self.documents.cancelled,
                                             self.documents.event, self.transport_fixture)

    @classmethod
    def search_fixture(cls, query, limit, cancelled):
        """Offer the same starting page to both prepared queries.

        Args: cls: Fixture class; query: Query; limit: Cap; cancelled: Stop callback.
        Returns: Search-shaped leads; records queries and never searches the web.
        """
        cancelled()
        cls.searches_seen.append(query)
        return {'provider': 'offline', 'results': [{'url': 'https://example.org/1',
                'title': 'First source', 'snippet': 'Not evidence'}]}

    @classmethod
    def transport_fixture(cls, url, limit, cancelled):
        """Serve safe pages with a successor and a link back to the first source.

        Args: cls: Fixture class; url: Requested URL; limit: Byte cap;
            cancelled: Stop callback.
        Returns: HTTP tuple; records actual source fetches, excluding robots reads.
        """
        cancelled()
        if url.endswith('/robots.txt'):
            return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
        cls.fetched.append(url)
        number = int(url.rsplit('/', 1)[-1])
        next_link = f'<a href="/{number+1}">Next Acme details</a>' if number < cls.page_limit else ''
        body = f'<title>Source {number}</title><p>Acme builds AI tools in Canada, page {number}.</p>{next_link}<a href="/1#back">Original source</a>'
        return 200, {'content-type': 'text/html'}, body.encode()


class ResearchRoundTests(unittest.TestCase):
    """Prove round limits, cumulative reporting, retry isolation and input bounds."""

    def setUp(self):
        """Reset external-fixture observations between independent tests.

        Args: None.
        Returns: None; prevents earlier test traffic from satisfying later checks.
        """
        RoundRun.searches_seen = []
        RoundRun.fetched = []
        RoundRun.page_limit = 9

    def run_question(self, client, rounds=2):
        """Start a question through the production endpoint and wait for completion.

        Args: client: Real application TestClient; rounds: Requested total rounds.
        Returns: Terminal saved state; asserts HTTP admission, not research quality.
        """
        sid = str(uuid4())
        response = client.post('/api/setup', json={'session_id': sid,
            'question': 'Which Canadian companies build AI tools?',
            'auto_advance': True, 'research_rounds': rounds})
        self.assertEqual(response.status_code, 202, response.text)
        return wait_for_completion(client, sid)

    def test_http_two_rounds_combine_reviewed_facts_and_preserve_inputs(self):
        """Follow one selected link and report facts from both original source stores.

        Args: None.
        Returns: None; verifies real stage artifacts, one search pair, no backward
            refetch, both rounds in report output and immutable cumulative inputs.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp); caller = RoundCaller(); sessions = Sessions(root, caller, runner_factory=RoundRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    state = self.run_question(client)
                    self.assertEqual(state['status'], 'report_completed', state.get('report'))
                    self.assertEqual(state['current_round'], 2)
                    self.assertEqual(len(state['round_history']), 1)
                    self.assertEqual(len(RoundRun.searches_seen), 2)
                    self.assertEqual(RoundRun.fetched, ['https://example.org/1', 'https://example.org/2'])
                    base = root/state['session_id']
                    for item in [*state['round_history'], state]:
                        for key in ('run', 'selection', 'triage', 'investigation', 'link_ranking'):
                            self.assertIn(item[key]['status'], ('completed', 'partial'))
                            self.assertTrue((base/item[key]['artifact_directory']).is_dir())
                    report = client.get(f"/api/setup/{state['session_id']}/report-data").json()
                    self.assertEqual(len(report['facts']), 2)
                    self.assertEqual(len(report['entries']), 1)
                    self.assertEqual({f['original_fact_id'].split(':')[0] for f in report['facts']}, {'round-1', 'round-2'})
                    manifest = json.loads((base/state['report']['artifact_directory']/'manifest.json').read_text())
                    self.assertEqual(len(manifest['packet']['coverage']['rounds']), 2)
                    self.assertEqual({r['research_round'] for r in manifest['original_records']}, {1, 2})
                    for original in manifest['original_records']:
                        self.assertIn('run_artifact_directory', original['source'])
                    count = len(caller.calls)
                    client.post(f"/api/setup/{state['session_id']}/report")
                    self.assertEqual(len(caller.calls), count)
            finally:
                sessions.close()

    def test_limits_one_five_and_empty_frontier(self):
        """Stop at configured limits, including beyond LangGraph's default depth.

        Args: None.
        Returns: None; five rounds finish, one round never follows links, and a
            cyclic-only frontier finishes early without interpreting it as failure.
        """
        for requested, available, expected in ((1, 9, 1), (5, 9, 5), (5, 1, 1)):
            with self.subTest(rounds=requested, available=available), TemporaryDirectory() as tmp:
                self.setUp(); RoundRun.page_limit = available
                sessions = Sessions(Path(tmp), RoundCaller(), runner_factory=RoundRun)
                try:
                    with TestClient(create_app(sessions)) as client:
                        state = self.run_question(client, requested)
                        self.assertEqual(state['status'], 'report_completed', state.get('report'))
                        self.assertEqual(state['current_round'], expected)
                        self.assertEqual(len(RoundRun.fetched), expected)
                        self.assertEqual(len(RoundRun.searches_seen), 2)
                        if available == 1:
                            self.assertEqual(state['rounds_finished_reason'], 'No unvisited selected links remain.')
                finally:
                    sessions.close()

    def test_report_retry_does_not_repeat_rounds(self):
        """Fail reporting after round two, then retry only the saved combined bucket.

        Args: None.
        Returns: None; all upstream files remain byte-identical, and successful
            retry makes no search, download, extraction, selection or review calls.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp); caller = RoundCaller(); caller.fail_report = True
            sessions = Sessions(root, caller, runner_factory=RoundRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    state = self.run_question(client)
                    self.assertEqual(state['status'], 'report_failed')
                    base = root/state['session_id']
                    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*')
                              if p.is_file() and p.name != 'session.json'}
                    before = len(caller.calls); fetched = list(RoundRun.fetched)
                    caller.fail_report = False
                    response = client.post(f"/api/setup/{state['session_id']}/report/retry",
                        json={'previous_started_at': state['report']['started_at']})
                    self.assertEqual(response.status_code, 202)
                    result = wait_for_completion(client, state['session_id'])
                    self.assertEqual(result['status'], 'report_completed')
                    self.assertEqual(result['current_round'], 2)
                    self.assertEqual(RoundRun.fetched, fetched)
                    self.assertTrue(all(stage.startswith('report-') for stage, _, _ in caller.calls[before:]))
                    for path, digest in hashes.items():
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, str(path))
            finally:
                sessions.close()

    def test_stop_and_retry_second_round_does_not_search_again(self):
        """Stop extraction in round two and retry its saved selected destinations.

        Args: None.
        Returns: None; round-one evidence survives, no third round appears, and
            explicit retry repeats only the incomplete second document attempt.
        """
        with TemporaryDirectory() as tmp:
            caller = RoundCaller(); sessions = Sessions(Path(tmp), caller, runner_factory=RoundRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    caller.stop_on_second = lambda: next(iter(sessions.stops.values())).set()
                    state = self.run_question(client)
                    self.assertEqual(state['status'], 'run_cancelled')
                    self.assertEqual(state['current_round'], 2)
                    first = state['round_history'][0]
                    caller.stop_on_second = None
                    response = client.post(f"/api/setup/{state['session_id']}/retry",
                        json={'previous_started_at': state['run']['started_at']})
                    self.assertEqual(response.status_code, 202)
                    result = wait_for_completion(client, state['session_id'])
                    self.assertEqual(result['status'], 'report_completed', result.get('report'))
                    self.assertEqual(result['round_history'][0], first)
                    self.assertEqual(len(RoundRun.searches_seen), 2)
                    self.assertEqual(RoundRun.fetched.count('https://example.org/1'), 1)
                    self.assertEqual(result['current_round'], 2)
            finally:
                sessions.close()

    def test_unselected_new_facts_never_enter_cumulative_report(self):
        """Exclude second-round extracted facts at selection and retain earlier evidence.

        Args: None.
        Returns: None; combined reporting includes only reviewed report knowledge,
            not every raw fact from documents that happened to be downloaded.
        """
        with TemporaryDirectory() as tmp:
            caller = RoundCaller(); caller.exclude_second = True
            sessions = Sessions(Path(tmp), caller, runner_factory=RoundRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    state = self.run_question(client)
                    self.assertEqual(state['status'], 'report_completed', state.get('report'))
                    self.assertEqual(state['run']['facts'], 1)
                    self.assertEqual(state['selection']['facts_selected'], 0)
                    self.assertEqual(state['report']['facts_total'], 1)
                    data = client.get(f"/api/setup/{state['session_id']}/report-data").json()
                    self.assertNotIn('page 2', str(data['facts']))
            finally:
                sessions.close()

    def test_redirect_alias_is_not_extracted_twice(self):
        """Discover an unknown alias back to the first round without duplicating facts.

        Args: None.
        Returns: None; the redirect receipt is retained, repeated extraction is
            skipped, and earlier reviewed evidence still produces the final report.
        """
        from unittest.mock import patch
        original = RoundRun.transport_fixture

        def redirect(url, limit, cancelled):
            """Redirect the second page to the already collected first destination.

            Args: url: Requested URL; limit: Byte cap; cancelled: Stop callback.
            Returns: Redirect or ordinary fixture response, without external I/O.
            """
            if url.endswith('/2'):
                return 302, {'location': '/1'}, b''
            return original(url, limit, cancelled)

        with TemporaryDirectory() as tmp, patch.object(RoundRun, 'transport_fixture', side_effect=redirect):
            caller = RoundCaller(); sessions = Sessions(Path(tmp), caller, runner_factory=RoundRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    state = self.run_question(client)
                    self.assertEqual(state['status'], 'report_completed', state.get('report'))
                    self.assertEqual(state['run']['sources'][0]['status'], 'already_downloaded')
                    self.assertEqual(state['run']['facts'], 0)
                    self.assertEqual(state['report']['facts_total'], 1)
                    self.assertEqual(state['current_round'], 2)
            finally:
                sessions.close()

    def test_setting_validation_and_default(self):
        """Reject malformed limits and expose the same bounded default in the form.

        Args: None.
        Returns: None; validates before work is scheduled; default is two rounds.
        """
        from projectclarity.ux.web_app import StartInput
        self.assertEqual(StartInput(session_id=uuid4(), question='Question').research_rounds, 2)
        with TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), RoundCaller(), runner_factory=RoundRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    for bad in (0, 6, True, 1.5, '2'):
                        response = client.post('/api/setup', json={'session_id': str(uuid4()),
                            'question': 'Question', 'research_rounds': bad})
                        self.assertEqual(response.status_code, 422)
                    self.assertIn('id="research-rounds"', client.get('/').text)
            finally:
                sessions.close()
