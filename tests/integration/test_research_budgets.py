"""Exercise budget enforcement through the real model client and setup HTTP path.

Only the external provider is simulated. Production accounting, reservations,
background setup, saved files, API decisions and cancellation remain real. These
checks establish admission and recovery, not provider billing or live answer quality.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from projectclarity.model_access.model_client import ModelCalls
from projectclarity.model_access.model_ledger import read_ledger, write_entry
from projectclarity.ux.web_app import create_app, StartInput
from projectclarity.workflow.research_sessions import Sessions
from tests.integration.test_research_setup import PLAN
from tests.unit.test_model_ledger import response

REQUEST = {'model': 'offline', 'max_tokens': 20, 'messages': []}


def eventually(check):
    """Wait briefly for a real background transition without starting extra work.

    Args: check: Side-effect-free callback returning a truthy result when ready.
    Returns: That result, or AssertionError after five seconds.
    """
    until = time.monotonic() + 5
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Budget transition did not finish')


def seed(root, calls=2, tokens=1000):
    """Save a minimal session so the production transport enforces its allowance.

    Args: root: Temporary sessions directory; calls: Request cap; tokens: Token cap.
    Returns: New UUID; writes no credentials and does not start a workflow.
    """
    sid = str(uuid4())
    (root / sid).mkdir()
    (root / sid / 'session.json').write_text(json.dumps({'session_id': sid,
        'status': 'working', 'model_request_budget': calls, 'model_token_budget': tokens}))
    return sid


class BudgetTests(unittest.TestCase):
    """Prove real admission, HTTP decisions and durable retry accounting offline."""

    def test_parallel_calls_cannot_spend_the_same_request_allowance(self):
        """Hold two provider calls while six real client workers contend for a cap.

        Args: None.
        Returns: None; verifies two admissions, draining before pause, explicit
            extension, no replay, and Quit releasing the remaining workers.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
            root = Path(tmp); sid = seed(root); caller = ModelCalls(root, root/'unused')
            messages = provider.return_value.__enter__.return_value.messages
            messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)
            entered = []; guard = Lock(); release = Event()

            def send(**request):
                """Hold provider execution to measure simultaneous admission.

                Args: request: Actual provider request, unused by the fake reply.
                Returns: Known-usage response after the test releases in-flight work.
                """
                with guard:
                    entered.append(request)
                if not release.wait(5):
                    raise AssertionError('Test provider was not released')
                return response()

            messages.create.side_effect = send
            pool = ThreadPoolExecutor(max_workers=6)
            futures = [pool.submit(caller, f'extract-{i}', REQUEST, sid) for i in range(6)]
            try:
                eventually(lambda: len(entered) == 2)
                snapshot = caller.budgets.snapshot(sid)
                self.assertEqual(snapshot['reserved_calls'], 2)
                self.assertFalse(snapshot['paused'])
                release.set()
                eventually(lambda: caller.budgets.snapshot(sid)['paused'])
                self.assertEqual(len(entered), 2)
                revision = caller.budgets.snapshot(sid)['revision']
                self.assertTrue(caller.budgets.decide(sid, revision, 'continue'))
                self.assertFalse(caller.budgets.decide(sid, revision, 'continue'))
                eventually(lambda: caller.budgets.snapshot(sid)['paused'])
                self.assertEqual(len(entered), 4)
                caller.budgets.decide(sid, caller.budgets.snapshot(sid)['revision'], 'quit')
                eventually(lambda: all(f.done() for f in futures))
                self.assertEqual(sum(f.exception() is None for f in futures), 4)
                self.assertEqual(read_ledger(root/sid)['totals']['attempted'], 4)
            finally:
                release.set(); caller.budgets.close(); pool.shutdown(wait=True)

    def test_token_reservations_refund_unused_output_and_include_cache(self):
        """Use a token cap smaller than two reservations but large enough after refund.

        Args: None.
        Returns: None; tests actual input/output/cache sums, waiting rather than
            spurious approval, and a pre-dispatch pause for the third request.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
            root = Path(tmp); sid = seed(root, calls=20, tokens=55); caller = ModelCalls(root, root/'unused')
            messages = provider.return_value.__enter__.return_value.messages
            messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)
            messages.create.return_value = response()  # 25 tokens, including 10 cache tokens.
            pool = ThreadPoolExecutor(max_workers=3)
            futures = [pool.submit(caller, f'select-{i}', REQUEST, sid) for i in range(3)]
            try:
                eventually(lambda: caller.budgets.snapshot(sid) and caller.budgets.snapshot(sid)['paused'])
                state = caller.budgets.snapshot(sid)
                self.assertEqual(messages.create.call_count, 2)
                self.assertEqual(state['known_tokens'], 50)
                self.assertIn('Token budget', state['reason'])
                caller.budgets.decide(sid, state['revision'], 'quit')
                eventually(lambda: all(f.done() for f in futures))
            finally:
                caller.budgets.close(); pool.shutdown(wait=True)

    def test_setup_http_pause_continue_and_quit_never_replay(self):
        """Hit the cap between definition and clarification through production HTTP.

        Args: None.
        Returns: None; tests saved settings, polling without replay, stale actions,
            defaults, input validation and Quit releasing setup without downstream work.
        """
        for action in ('continue', 'quit'):
            with self.subTest(action=action), TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
                root = Path(tmp); messages = provider.return_value.__enter__.return_value.messages
                messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)
                messages.create.side_effect = [response(json.dumps(PLAN)), response(json.dumps({'questions': []}))]
                sessions = Sessions(root); sid = str(uuid4())
                try:
                    with TestClient(create_app(sessions)) as client:
                        request = {'session_id': sid, 'question': 'Canadian companies', 'model_request_budget': 1, 'model_token_budget': 100000}
                        self.assertEqual(client.post('/api/setup', json=request).status_code, 202)
                        eventually(lambda: sessions.read(sid).get('model_budget', {}).get('paused'))
                        state = client.get(f'/api/setup/{sid}').json()
                        self.assertEqual(state['model_request_budget'], 1)
                        self.assertEqual(messages.create.call_count, 1)
                        revision = state['model_budget']['revision']
                        self.assertEqual(client.post(f'/api/setup/{sid}/budget', json={'revision': revision+1, 'action': 'continue'}).status_code, 200)
                        self.assertTrue(sessions.read(sid)['model_budget']['paused'])
                        self.assertEqual(client.post(f'/api/setup/{sid}/budget', json={'revision': revision, 'action': action}).status_code, 200)
                        eventually(lambda: sessions.read(sid)['status'] != 'working')
                        self.assertEqual(messages.create.call_count, 2 if action == 'continue' else 1)
                        self.assertEqual(sessions.read(sid)['status'], 'ready' if action == 'continue' else 'failed')
                        bad = dict(request, session_id=str(uuid4()), model_request_budget=0)
                        self.assertEqual(client.post('/api/setup', json=bad).status_code, 422)
                        bad['model_request_budget'] = 1; bad['model_token_budget'] = 1.5
                        self.assertEqual(client.post('/api/setup', json=bad).status_code, 422)
                finally:
                    sessions.close()
        settings = StartInput(session_id=uuid4(), question='x')
        self.assertEqual((settings.model_request_budget, settings.model_token_budget), (750, 8_000_000))

    def test_http_autonomous_workflow_shares_allowance_through_report(self):
        """Run real setup, extraction, selection, review and reporting under one cap.

        Args: None.
        Returns: None; proves the production graph's model calls share accounting
            and Continue resumes the held stage, without replaying setup/extraction.
            Search/download and provider replies are offline fixtures only.
        """
        from tests.integration.test_document_pipeline import FixtureRun
        from tests.integration.test_research_graph import FullWorkflowCaller
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
            root = Path(tmp); sid = str(uuid4()); fixtures = FullWorkflowCaller(questions=[])
            messages = provider.return_value.__enter__.return_value.messages
            messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)

            def send(**request):
                """Return schema-appropriate fixtures at the provider boundary only.

                Args: request: Exact payload emitted by the real stage and client.
                Returns: Complete provider-shaped reply with known usage counters.
                """
                properties = request['output_config']['format']['schema']['properties']
                stage = next(stage for key, stage in [
                    ('information_needed', 'define'), ('questions', 'clarify'),
                    ('searches', 'search_queries'), ('entities', 'extract-fixture'),
                    ('selected_facts', 'evidence-fixture'), ('decisions', 'triage-fixture'),
                    ('items', 'report-fixture')] if key in properties)
                return response(fixtures(stage, request, sid))

            messages.create.side_effect = send
            sessions = Sessions(root, runner_factory=FixtureRun)
            try:
                with TestClient(create_app(sessions)) as client:
                    result = client.post('/api/setup', json={'session_id': sid, 'question': 'Canadian AI companies',
                        'auto_advance': True, 'research_rounds': 1, 'model_request_budget': 4, 'model_token_budget': 100000})
                    self.assertEqual(result.status_code, 202)
                    eventually(lambda: sessions.read(sid).get('model_budget', {}).get('paused'))
                    state = sessions.read(sid)
                    self.assertEqual(state['status'], 'selection_running')
                    self.assertEqual(messages.create.call_count, 4)
                    client.post(f'/api/setup/{sid}/budget', json={'revision': state['model_budget']['revision'], 'action': 'continue'})
                    eventually(lambda: sessions.read(sid)['status'] == 'report_completed')
                    state = sessions.read(sid)
                    self.assertEqual(state['model_budget']['calls_used'], messages.create.call_count)
                    self.assertEqual(state['model_budget']['known_tokens'], 25 * messages.create.call_count)
                    stages = [call[0] for call in fixtures.calls]
                    self.assertEqual(stages.count('define'), 1)
                    self.assertEqual(stages.count('extract-fixture'), 1)
                    self.assertIn('report-fixture', stages)
            finally:
                sessions.close()

    def test_restart_and_retry_keep_usage_and_charge_uncertain_attempts(self):
        """Recover a crash between dispatch and settlement without erasing usage.

        Args: None.
        Returns: None; ensures the next transport pauses before sending, retains
            a conservative estimate and permits only an acknowledged continuation.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
            root = Path(tmp); sid = seed(root, calls=2, tokens=1000)
            first = ModelCalls(root, root/'unused')
            first.budgets.reserve(sid, 'interrupted', 30)
            write_entry(root/sid, {'call_id': 'interrupted', 'stage': 'extract', 'stage_group': 'Extraction',
                'attempted': True, 'usage': None, 'outcome': 'running', 'validation': 'unknown'})
            restarted = ModelCalls(root, root/'unused')
            messages = provider.return_value.__enter__.return_value.messages
            messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)
            messages.create.return_value = response()
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(restarted, 'report-selection', REQUEST, sid)
            try:
                eventually(lambda: restarted.budgets.snapshot(sid)['paused'])
                state = restarted.budgets.snapshot(sid)
                self.assertEqual((state['calls_used'], state['estimated_tokens'], state['unknown_usage']), (1, 30, 1))
                self.assertEqual(messages.create.call_count, 0)
                restarted.budgets.decide(sid, state['revision'], 'continue')
                future.result(timeout=5)
                state = restarted.budgets.snapshot(sid)
                self.assertEqual((state['calls_used'], state['known_tokens'], state['estimated_tokens']), (2, 25, 30))
            finally:
                restarted.budgets.close(); pool.shutdown(wait=True)

    def test_shutdown_unblocks_pause_and_read_only_snapshot_does_not_write(self):
        """Ensure paused shutdown cannot deadlock the server or silently grant room.

        Args: None.
        Returns: None; checks zero provider generations and unchanged read-only files.
        """
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'fixture'}), patch('projectclarity.model_access.model_client.Anthropic') as provider:
            root = Path(tmp); sid = seed(root, tokens=1); caller = ModelCalls(root, root/'unused')
            messages = provider.return_value.__enter__.return_value.messages
            messages.count_tokens.return_value = SimpleNamespace(input_tokens=10)
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(caller, 'define', REQUEST, sid)
            try:
                eventually(lambda: caller.budgets.snapshot(sid) and caller.budgets.snapshot(sid)['paused'])
                path = root/sid/'model-budget.json'; before = path.read_bytes()
                caller.budgets.snapshot(sid)
                self.assertEqual(path.read_bytes(), before)
                caller.budgets.close()
                with self.assertRaises(RuntimeError):
                    future.result(timeout=5)
                self.assertEqual(messages.create.call_count, 0)
            finally:
                caller.budgets.close(); pool.shutdown(wait=True)
