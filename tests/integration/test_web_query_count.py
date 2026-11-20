"""Check that the saved query-count setting reaches planning and actual search dispatch.

The HTTP workflow, validation and search coordinator are real. Search, document
HTTP and model replies use deterministic fixtures, so these checks spend no model
budget and do not claim real-world search coverage.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from projectclarity.ux.web_app import create_app
from projectclarity.workflow.research_sessions import Sessions
from projectclarity.workflow.research_graph import validate_searches
from tests.integration.test_document_pipeline import FixtureRun
from tests.integration.test_research_graph import FullWorkflowCaller, wait_for_completion


class QueryCountCaller(FullWorkflowCaller):
    """Return the requested number of distinct query fixtures."""

    def __call__(self, stage, request, sid):
        """Honor the actual query count passed to the model-facing request.

        Args:
            stage: Actual model stage.
            request: Production request, including finalized scope and query count.
            sid: Owning session identifier.
        Returns: JSON containing exactly the requested queries for search preparation;
            other stages use existing fixture responses. Captures all requests.
        """
        if stage == 'search_queries':
            self.calls.append((stage, request, sid))
            count = json.loads(request['messages'][0]['content'])['web_query_count']
            return json.dumps({'searches': [{'query': f'Acme research angle {index}',
                'purpose': f'Distinct fixture angle {index}'} for index in range(count)]})
        return super().__call__(stage, request, sid)


class WebQueryCountTests(unittest.TestCase):
    """Prove default compatibility, chosen counts and malformed-input rejection."""

    def test_requested_count_reaches_saved_planning_and_search_dispatch(self):
        """Execute one, default two, and four searches through the real HTTP workflow.

        Args: None.
        Returns: None; verifies saved settings, requested model count, actual search
            receipts, URL allowance and automatic report completion without a graph gate.
        """
        for count in (1, None, 4):
            with self.subTest(count=count), TemporaryDirectory() as tmp:
                root = Path(tmp)
                caller = QueryCountCaller(questions=[])
                sessions = Sessions(root, caller, env_file=root/'absent.env', runner_factory=FixtureRun)
                sid = str(uuid4())
                try:
                    with TestClient(create_app(sessions)) as client:
                        payload = {'session_id': sid, 'question': 'Which companies build tools?',
                                   'auto_advance': True, 'research_rounds': 1}
                        if count is not None:
                            payload['web_query_count'] = count
                        self.assertEqual(client.post('/api/setup', json=payload).status_code, 202)
                        state = wait_for_completion(client, sid)
                        self.assertEqual(state['status'], 'report_completed')
                        expected = count or 2
                        self.assertEqual(state['web_query_count'], expected)
                        self.assertEqual(len(state['searches']), expected)
                        self.assertEqual(len(state['run']['searches']), expected)
                        self.assertEqual(state['run']['limits']['max_urls'], 5 * expected)
                        folder = root / sid / state['run']['artifact_directory']
                        manifest = json.loads((folder/'search-manifest.json').read_text())
                        self.assertEqual(manifest['query_count'], expected)
                        self.assertEqual(len(list(folder.glob('search-[0-9].json'))), expected)
                        request = next(request for stage, request, _ in caller.calls if stage == 'search_queries')
                        self.assertEqual(json.loads(request['messages'][0]['content'])['web_query_count'], expected)
                finally:
                    sessions.close()

    def test_count_bounds_and_model_count_mismatch(self):
        """Reject unsupported settings and wrong-sized query lists before dispatch.

        Args: None.
        Returns: None; API rejects booleans, strings, zero and excessive counts;
            validator rejects too few/many queries for a valid requested count.
        """
        with TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), QueryCountCaller(questions=[]), env_file=Path(tmp)/'absent.env')
            try:
                with TestClient(create_app(sessions)) as client:
                    for count in (0, 11, True, '3'):
                        self.assertEqual(client.post('/api/setup', json={'session_id': str(uuid4()),
                            'question': 'Test', 'web_query_count': count}).status_code, 422)
                text = json.dumps({'searches': [{'query': 'Acme', 'purpose': 'Find Acme'}]})
                self.assertEqual(len(validate_searches(text, 1).searches), 1)
                with self.assertRaises(ValueError):
                    validate_searches(text, 3)
            finally:
                sessions.close()
