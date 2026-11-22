"""Prove collection boundaries through HTTP, real research routing and real Neo4j.

Ordinary tests need no database. Set CLARITY_TEST_NEO4J_ENV to a private connection
file to enable the real database test. It creates unique test collections and does
not touch existing research. Search, download and model replies remain fixtures;
Neo4j persistence, Cypher, application routing and extraction validation are real.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.ux.web_app import create_app
from tests.integration.test_research_graph import FullWorkflowCaller, wait_for_completion
from tests.integration.test_document_pipeline import FixtureRun


class RelationshipCaller(FullWorkflowCaller):
    """Return an explicit organization-to-place relationship for the fixture text."""

    def __call__(self, stage, request, sid):
        """Supply a source-grounded relationship without changing production routing.

        Args:
            stage: Actual model stage name.
            request: Unchanged production request.
            sid: Research session identifier.
        Returns: JSON fixture response. Records calls through the parent caller;
            only extraction payloads receive the explicit Canada object.
        """
        response = super().__call__(stage, request, sid)
        payload = json.loads(response)
        if 'entities' in payload:
            payload['entities'].append({'id': 'canada', 'name': 'Canada', 'type': 'place', 'subtype': None})
            payload['facts'][0].update(predicate='builds tools in', object_id='canada', value='Canada')
        return json.dumps(payload)


class KnowledgeGraphTests(unittest.TestCase):
    """Check input boundaries without depending on a database installation."""

    def test_collection_validation_and_safe_unconfigured_failure(self):
        """Keep collection choice durable and reject invalid or cross-site writes.

        Args: None.
        Returns: None; asserts HTTP contracts and absence of credential-bearing errors.
        """
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            sessions = Sessions(Path(tmp), FullWorkflowCaller(questions=[]), env_file=Path(tmp)/'absent')
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post('/api/collections', json={'name': '  '}).status_code, 422)
                    row = client.post('/api/collections', json={'name': 'Fiction'}).json()
                    self.assertEqual(client.get('/api/collections').json(), [sessions.default_collection, row])
                    self.assertEqual(sessions.collections.default(), sessions.default_collection)
                    self.assertEqual(client.get('/api/knowledge-graph').status_code, 503)
                    self.assertNotIn('create-collection', client.get('/graph').text)
                    self.assertIn('id="knowledge-session"', client.get('/').text)
                    self.assertEqual(client.post('/api/collections', json={'name': 'No'}, headers={'origin': 'https://example.org'}).status_code, 403)
                    response = client.get(f"/api/collections/{row['id']}/graph")
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn('password', response.text.lower())
                    self.assertEqual(client.get(f"/api/collections/{uuid4()}/graph").status_code, 404)
                    self.assertEqual(client.get(f"/api/collections/{row['id']}/graph?limit=501").status_code, 422)
            finally:
                sessions.close()

    def test_fresh_default_explicit_reuse_and_idempotent_setup(self):
        """Create isolated knowledge sessions through HTTP unless reuse is explicit.

        Args: None; uses fixture model responses and no Neo4j connection.
        Returns: None; asserts fresh IDs, unchanged duplicate starts, existing-ID
            reuse, rejected unknown/reassigned IDs, and persistent membership.
        """
        with TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), FullWorkflowCaller(questions=[]), env_file=Path(tmp)/'absent')
            try:
                with TestClient(create_app(sessions)) as client:
                    states = []
                    for _ in range(2):
                        payload = {'session_id': str(uuid4()), 'question': 'Where does Acme build tools?'}
                        response = client.post('/api/setup', json=payload)
                        self.assertEqual(response.status_code, 202)
                        states.append(response.json())
                        sessions.executor.submit(lambda: None).result(timeout=20)
                        before = client.get('/api/collections').json()
                        duplicate = client.post('/api/setup', json=payload)
                        self.assertEqual(duplicate.json()['collection_id'], states[-1]['collection_id'])
                        self.assertEqual(client.get('/api/collections').json(), before)
                    first, second = [state['collection_id'] for state in states]
                    self.assertNotEqual(first, second)
                    self.assertNotIn(sessions.default_collection['id'], [first, second])
                    payload = {'session_id': str(uuid4()), 'question': 'Where does Acme build tools?',
                               'collection_id': first}
                    self.assertEqual(client.post('/api/setup', json=payload).json()['collection_id'], first)
                    sessions.executor.submit(lambda: None).result(timeout=20)
                    self.assertEqual(client.post('/api/setup', json={**payload, 'collection_id': second}).status_code, 409)
                    self.assertEqual(client.post('/api/setup', json={**payload, 'session_id': str(uuid4()),
                                     'collection_id': str(uuid4())}).status_code, 404)
                    self.assertEqual(len(client.get(f'/api/collections/{first}/sessions').json()), 2)
                    self.assertEqual(len(client.get(f'/api/collections/{second}/sessions').json()), 1)
                    self.assertEqual(client.get('/api/knowledge-graph/sessions').json(), [])
                sessions.close()
                sessions = Sessions(Path(tmp), FullWorkflowCaller(questions=[]), env_file=Path(tmp)/'absent')
                self.assertEqual(sessions.read(states[0]['session_id'])['collection_id'], first)
            finally:
                sessions.close()

    @unittest.skipUnless(os.environ.get('CLARITY_TEST_NEO4J_ENV'), 'Real Neo4j connection not supplied')
    def test_fresh_graphs_stay_isolated_until_explicit_reuse(self):
        """Prove fresh default isolation and chosen accumulation in actual Neo4j.

        Args: None; uses the private test database configuration and fixture models.
        Returns: None; runs three production HTTP workflows with identical names,
            checks final persisted assertion membership and an empty legacy default.
            New UUID collections remain for inspection; existing graph data is untouched.
        """
        with TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), RelationshipCaller(questions=[]),
                env_file=Path(os.environ['CLARITY_TEST_NEO4J_ENV']), runner_factory=FixtureRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    states = []
                    for index in range(3):
                        sid = str(uuid4())
                        payload = {'session_id': sid, 'question': 'Where does Acme build tools?',
                                   'auto_advance': True, 'research_rounds': 1}
                        if index == 2:
                            payload['collection_id'] = states[0]['collection_id']
                        self.assertEqual(client.post('/api/setup', json=payload).status_code, 202)
                        self.assertEqual(wait_for_completion(client, sid)['status'], 'report_completed')
                        sessions.graph_jobs.executor.submit(lambda: None).result(timeout=30)
                        state = client.get(f'/api/setup/{sid}').json()
                        self.assertEqual(state['knowledge_graph']['status'], 'completed')
                        states.append(state)
                    self.assertNotEqual(states[0]['collection_id'], states[1]['collection_id'])
                    for index, expected in [(0, {states[0]['session_id'], states[2]['session_id']}),
                                            (1, {states[1]['session_id']})]:
                        cid = states[index]['collection_id']
                        rows = client.get(f'/api/collections/{cid}/graph').json()['rows']
                        self.assertEqual({row['assertion']['session_id'] for row in rows}, expected)
                        self.assertEqual(len(rows), len(expected))
                    self.assertEqual(client.get('/api/knowledge-graph').json()['rows'], [])
            finally:
                sessions.close()

    @unittest.skipUnless(os.environ.get('CLARITY_TEST_NEO4J_ENV'), 'Real Neo4j connection not supplied')
    def test_production_http_accumulation_isolation_retry_and_restart(self):
        """Use actual Neo4j for two explicitly shared sessions and one isolated session.

        Args: None; reads only the private connection-file path from the environment.
        Returns: None; asserts automatic projection, explicit relationships, quotes,
            explicit default reuse, isolation, parameterized filtering, retry idempotence, rollback after
            tampered evidence, and persistence after reopening the application.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = Path(os.environ['CLARITY_TEST_NEO4J_ENV'])
            sessions = Sessions(root, RelationshipCaller(questions=[]), env_file=env_file, runner_factory=FixtureRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    collections = [sessions.default_collection['id'],
                                   client.post('/api/collections', json={'name': 'Graph test fiction'}).json()['id']]
                    ids = []
                    for cid in (collections[0], collections[0], collections[1]):
                        sid = str(uuid4()); ids.append(sid)
                        response = client.post('/api/setup', json={'session_id': sid,
                            'collection_id': cid,
                            'question': 'Where does Acme build tools?', 'auto_advance': True, 'research_rounds': 1})
                        self.assertEqual(response.status_code, 202)
                        self.assertEqual(wait_for_completion(client, sid)['status'], 'report_completed')
                        sessions.executor.submit(lambda: None).result(timeout=30)
                        sessions.graph_jobs.executor.submit(lambda: None).result(timeout=30)
                        state = client.get(f'/api/setup/{sid}').json()
                        self.assertEqual(state['knowledge_graph']['status'], 'completed', state.get('knowledge_graph'))
                        self.assertEqual(state['knowledge_graph']['assertions'], 1)
                    url = f'/api/collections/{collections[0]}/graph'
                    graph = client.get(url).json()
                    self.assertEqual(len(graph['rows']), 2)
                    self.assertEqual(client.get('/api/knowledge-graph').json(), graph)
                    self.assertEqual(len(client.get('/api/knowledge-graph/sessions').json()), 2)
                    self.assertEqual({r['assertion']['session_id'] for r in graph['rows']}, set(ids[:2]))
                    for row in graph['rows']:
                        self.assertEqual(row['object']['name'], 'Canada')
                        self.assertEqual(row['assertion']['quote'], 'Acme builds AI tools in Canada.')
                        self.assertTrue(row['assertion']['quote_rechecked'])
                        self.assertEqual(row['subject']['collection_id'], collections[0])
                    self.assertTrue(client.get(url+'?limit=1').json()['has_more'])
                    self.assertEqual(client.get(url, params={'search': "' OR true //"}).json()['rows'], [])
                    self.assertEqual(client.get(url, params={'session_id': ids[2]}).json()['rows'], [])
                    self.assertEqual(client.post(f'/api/setup/{ids[0]}/graph-sync').json()['assertions'], 1)
                    self.assertEqual(len(client.get(url).json()['rows']), 2)
                    state = sessions.read(ids[0]); folder = root / ids[0] / state['run']['artifact_directory']
                    index = json.loads((folder/'knowledge.json').read_text())
                    receipt = folder/index['chunks'][0]['artifact']
                    original = receipt.read_bytes(); receipt.write_bytes(original+b' ')
                    self.assertEqual(client.post(f'/api/setup/{ids[0]}/graph-sync').json()['status'], 'failed')
                    self.assertEqual(len(client.get(url).json()['rows']), 2)
                    receipt.write_bytes(original)
                sessions.close()
                sessions = Sessions(root, RelationshipCaller(questions=[]), env_file=env_file, runner_factory=FixtureRun)
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(sessions.default_collection['id'], collections[0])
                    self.assertEqual(len(client.get('/api/knowledge-graph').json()['rows']), 2)
                    self.assertEqual(len(client.get(url).json()['rows']), 2)
                    self.assertEqual(len(client.get(f'/api/collections/{collections[1]}/graph').json()['rows']), 1)
            finally:
                sessions.close()

    @unittest.skipUnless(os.environ.get('CLARITY_TEST_NEO4J_ENV'), 'Real Neo4j connection not supplied')
    def test_slow_or_failed_graph_never_blocks_report(self):
        """Hold graph work at a barrier and prove reports complete independently.

        Args: None; uses real Neo4j when the held projection is released.
        Returns: None; asserts reporting completes while graph status is running,
            a later graph failure cannot rewrite report completion, and no manual
            report action is needed even for a stale browser's old pause flag.
        """
        for fail in (False, True):
            with self.subTest(fail=fail), TemporaryDirectory() as tmp:
                root = Path(tmp)
                sessions = Sessions(root, RelationshipCaller(questions=[]),
                    env_file=Path(os.environ['CLARITY_TEST_NEO4J_ENV']), runner_factory=FixtureRun)
                entered, release = Event(), Event()
                original = sessions.collections.sync

                def held_projection(state):
                    """Block only graph storage so the report must run around it.

                    Args: state: Frozen source snapshot supplied to the graph worker.
                    Returns: Real projection counts, or an intentional failure after
                        release. A timeout fails the test instead of silently proceeding.
                    """
                    entered.set()
                    if not release.wait(20):
                        raise AssertionError('Graph barrier was not released')
                    if fail:
                        raise RuntimeError('Deliberately unavailable graph')
                    return original(state)

                sid = str(uuid4())
                try:
                    with patch.object(sessions.collections, 'sync', side_effect=held_projection), TestClient(create_app(sessions)) as client:
                        client.post('/api/setup', json={'session_id': sid, 'question': 'Where does Acme build tools?',
                            'auto_advance': True, 'explore_before_report': True, 'research_rounds': 1})
                        self.assertTrue(entered.wait(10))
                        state = wait_for_completion(client, sid)
                        self.assertEqual(state['status'], 'report_completed')
                        self.assertEqual(state['knowledge_graph']['status'], 'running')
                        self.assertIn('report', state)
                        release.set()
                        sessions.graph_jobs.executor.submit(lambda: None).result(timeout=30)
                        state = client.get(f'/api/setup/{sid}').json()
                        self.assertEqual(state['status'], 'report_completed')
                        self.assertEqual(state['knowledge_graph']['status'], 'failed' if fail else 'completed')
                finally:
                    release.set()
                    sessions.close()

    @unittest.skipUnless(os.environ.get('CLARITY_TEST_NEO4J_ENV'), 'Real Neo4j connection not supplied')
    def test_graph_available_while_report_is_still_running(self):
        """Hold report I/O and inspect the independently completed real database view.

        Args: None; external research/model responses are fixtures, Neo4j is real.
        Returns: None; asserts the explorer and refresh work during reporting,
            neither starts extra report calls, and report completion retains graph status.
        """
        entered, release = Event(), Event()

        class HeldReportCaller(RelationshipCaller):
            """Delay only the final report model fixture, not graph preparation."""

            def __call__(self, stage, request, sid):
                """Expose report overlap while preserving ordinary fixture responses.

                Args:
                    stage: Requested model stage.
                    request: Unmodified production request.
                    sid: Owning session UUID.
                Returns: Parent fixture reply after report release; a missed release
                    raises instead of making the test appear to complete normally.
                """
                if stage.startswith('report-'):
                    entered.set()
                    if not release.wait(20):
                        raise AssertionError('Report barrier was not released')
                return super().__call__(stage, request, sid)

        with TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), HeldReportCaller(questions=[]),
                env_file=Path(os.environ['CLARITY_TEST_NEO4J_ENV']), runner_factory=FixtureRun)
            sid = str(uuid4())
            try:
                with TestClient(create_app(sessions)) as client:
                    client.post('/api/setup', json={'session_id': sid, 'question': 'Where does Acme build tools?',
                        'auto_advance': True, 'research_rounds': 1})
                    self.assertTrue(entered.wait(10))
                    sessions.graph_jobs.executor.submit(lambda: None).result(timeout=30)
                    state = client.get(f'/api/setup/{sid}').json()
                    self.assertEqual(state['status'], 'report_running')
                    self.assertEqual(state['knowledge_graph']['status'], 'completed')
                    rows = client.get(f"/api/collections/{state['collection_id']}/graph").json()['rows']
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['assertion']['quote'], 'Acme builds AI tools in Canada.')
                    self.assertNotIn('generate-report', client.get(f'/graph?session={sid}').text)
                    self.assertEqual(client.post(f'/api/setup/{sid}/graph-sync').json()['status'], 'completed')
                    self.assertEqual(client.get(f'/api/setup/{sid}').json()['status'], 'report_running')
                    release.set()
                    self.assertEqual(wait_for_completion(client, sid)['status'], 'report_completed')
                    self.assertEqual(client.get(f'/api/setup/{sid}').json()['knowledge_graph']['status'], 'completed')
            finally:
                release.set()
                sessions.close()
