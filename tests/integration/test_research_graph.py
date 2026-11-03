"""Check that a question travels through the actual research workflow to a report.

The tests submit a question through the web application and observe the order in
which the workflow runs. Search, download and AI replies are supplied locally; the
application still chooses and performs its real steps. Additional checks make sure
failed or cancelled selection stops downstream work and every page can load its
files. This verifies the connections between steps, not real-world research quality.
"""
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler

from projectclarity.workflow.research_graph import build_research_graph
from projectclarity.workflow.research_sessions import Sessions, ACTIVE_STATUSES
from projectclarity.ux.web_app import create_app
from tests.integration.test_document_pipeline import ExtractCaller, FixtureRun
from tests.integration.test_research_setup import FakeCaller
from tests.integration.test_evidence_selection_api import SelectorCaller, seed


class FullWorkflowCaller(ExtractCaller):
    """Serve deterministic setup and evidence responses without replacing graph work."""

    def __call__(self, stage, request, sid):
        """Choose the provider fixture appropriate to the actual requested stage.

        Args:
            stage: Model label produced by the real workflow.
            request: Unchanged model request to capture and answer.
            sid: Owning session UUID.
        Returns:
            Fixture JSON for setup or downstream research; both helpers record
            the exact request. No provider connection or stage routing is performed.
        """
        if stage in ('define', 'clarify', 'refine', 'search_queries'):
            return FakeCaller.__call__(self, stage, request, sid)
        return super().__call__(stage, request, sid)


class NodeTrace(BaseCallbackHandler):
    """Observe graph execution without changing any node's inputs or outputs."""

    def __init__(self):
        """Create an empty ordered trace for one test invocation.

        Args: None.
        Returns: None; no global callbacks or network activity are configured.
        """
        self.nodes = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        """Record named research nodes when LangGraph actually enters them.

        Args:
            serialized: Framework description; not used to infer execution.
            inputs: Actual node input, left unchanged.
            kwargs: Callback metadata containing the entered runnable's name.
        Returns:
            None; appends only known node names to this test's local trace.
        """
        name = kwargs.get('name')
        if name in ('documents', 'search', 'download', 'extract', 'selection',
                    'triage', 'investigation', 'link_ranking', 'report'):
            self.nodes.append(name)


def wait_for_completion(client, sid):
    """Poll the real public state until the offline worker finishes.

    Args:
        client: TestClient serving the production HTTP application.
        sid: Owning session UUID.
    Returns:
        Terminal snapshot, or AssertionError after ten seconds. Does not enqueue
        work; polling uses only the ordinary read-only status endpoint.
    """
    for _ in range(1000):
        state = client.get(f'/api/setup/{sid}').json()
        if state['status'] not in ACTIVE_STATUSES:
            return state
        time.sleep(.01)
    raise AssertionError('Research graph did not settle')


class ResearchGraphTests(unittest.TestCase):
    """Prove HTTP work uses graph edges and renamed files remain reachable."""

    def test_http_runs_real_nodes_in_order_and_serves_every_asset(self):
        """Trace the full autonomous pipeline and inspect all delivered page assets.

        Args: None.
        Returns:
            None; asserts actual graph execution order, final report, unchanged
            provider call count and successful delivery of every linked JS/CSS file.
        """
        trace = NodeTrace()

        def observed_graph(*args, **kwargs):
            """Attach observation to the production graph, without replacing its work.

            Args:
                args: Unmodified positional arguments from Sessions.
                kwargs: Unmodified entry-point and document-runner options.
            Returns:
                The actual compiled graph with an additional local callback.
            """
            return build_research_graph(*args, **kwargs).with_config(callbacks=[trace])

        with TemporaryDirectory() as tmp, patch('projectclarity.workflow.research_sessions.build_research_graph', side_effect=observed_graph):
            root = Path(tmp)
            caller = FullWorkflowCaller(questions=[])
            sessions = Sessions(root, caller, runner_factory=FixtureRun)
            sid = str(uuid4())
            try:
                with TestClient(create_app(sessions)) as client:
                    self.assertEqual(client.post('/api/setup', json={'session_id': sid,
                        'question': 'Which Canadian companies build AI tools?', 'auto_advance': True}).status_code, 202)
                    state = wait_for_completion(client, sid)
                    self.assertEqual(state['status'], 'report_completed')
                    self.assertEqual(trace.nodes, ['search', 'documents', 'download', 'extract',
                                                  'selection', 'triage', 'investigation', 'link_ranking', 'report'])
                    self.assertEqual(len(caller.calls), 7)
                    self.assertEqual([stage for stage, _, _ in caller.calls[:3]],
                                     ['define', 'clarify', 'search_queries'])
                    for page in ('/', f'/stage/{sid}', f'/run/{sid}', f'/evidence/{sid}',
                                 f'/triage/{sid}', f'/investigation/{sid}', f'/links/{sid}', f'/report/{sid}'):
                        response = client.get(page)
                        self.assertEqual(response.status_code, 200, page)
                        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
                        self.assertTrue(assets, page)
                        for asset in assets:
                            self.assertEqual(client.get(asset).status_code, 200, asset)
                    self.assertEqual(client.get(f'/api/setup/{sid}/report-data').status_code, 200)
            finally:
                sessions.close()

    def test_failed_or_cancelled_selection_never_reaches_triage(self):
        """Exercise graph stopping gates through the saved-evidence HTTP action.

        Args: None.
        Returns:
            None; all-invalid selection stops failed, and cancellation stops without
            downstream model calls. Original extraction records remain intact.
        """
        for cancel in (False, True):
            with self.subTest(cancel=cancel), TemporaryDirectory() as tmp:
                root = Path(tmp)
                state, hashes = seed(root)
                sid = state['session_id']
                caller = SelectorCaller(['invalid'])
                sessions = Sessions(root, caller)
                sessions.write(state)
                original_call = caller.__class__.__call__

                def controlled_call(instance, stage, request, session_id):
                    """Set cancellation at the real provider boundary when requested.

                    Args:
                        instance: Fixture provider retaining captured requests.
                        stage: Actual model call label.
                        request: Actual request left unchanged.
                        session_id: Session receiving the model response.
                    Returns:
                        Fixture response text; optionally sets the production stop
                        event after the request begins. No stage logic is replaced.
                    """
                    result = original_call(instance, stage, request, session_id)
                    if cancel:
                        sessions.stops[sid].set()
                    return result

                try:
                    with patch.object(SelectorCaller, '__call__', controlled_call), TestClient(create_app(sessions)) as client:
                        self.assertEqual(client.post(f'/api/setup/{sid}/selection').status_code, 202)
                        result = wait_for_completion(client, sid)
                        self.assertEqual(result['status'], 'selection_cancelled' if cancel else 'selection_failed')
                        self.assertNotIn('triage', result)
                        self.assertTrue(all(stage.startswith('evidence-') for stage, _, _ in caller.calls))
                        import hashlib
                        for path, digest in hashes.items():
                            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
                finally:
                    sessions.close()
