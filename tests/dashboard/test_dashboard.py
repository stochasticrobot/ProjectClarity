"""Check the local dashboard API against research workers and saved run outputs.

Tests start an isolated loopback server and launch the normal command-line worker
with synthetic provider responses. They inspect report design, findings, usage,
clarification, cancellation, source changes and run isolation through HTTP. Smaller
checks also exercise token accounting and request validation. Passing verifies
those application interactions, not browser appearance or live research accuracy.
"""
import base64
import copy
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from projectclarity.dashboard.metrics import token_usage
from projectclarity.dashboard.projection import snapshot
from projectclarity.dashboard.server import make_server
from tests.extraction.extraction_test_fixtures import SOURCE_TEXT


class DashboardTests(unittest.TestCase):
    """Run HTTP controls and read projections against isolated real research artifacts."""

    def setUp(self):
        """Start an isolated loopback server with transport-only worker substitution.

        Args:
            self: Test owner retaining server, process and filesystem cleanup.
        Returns:
            None. No real provider request can occur in the synthetic worker.
        """
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runs'
        self.server = make_server(self.root, 0)
        self.server.app.launcher = self.fixture_launcher
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        """Stop test workers and release HTTP sockets before temporary files disappear.

        Args:
            self: Test owner with child handles.
        Returns:
            None. Terminates only this test's unfinished children, never user runs.
        """
        for process in self.server.app.processes.values():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def fixture_launcher(self, command, **options):
        """Keep production arguments intact while installing the fake provider in a child.

        Args:
            self: Test instance owning the isolated server, workers and assertions.
            command: Production worker command; its executable/module prefix is checked.
            **options: Production process options, including private logs and no shell.
        Returns:
            Child handle. The fixture then runs real projectclarity.__main__.
        """
        self.assertEqual(command[:3], [sys.executable, '-m', 'projectclarity'])
        self.assertNotIn('shell', options)
        if 'research' in command:
            self.assertEqual(command[command.index('--model-concurrency') + 1], '3')
        return subprocess.Popen([*command[:2], 'tests.dashboard.worker_fixture', *command[3:]], **options)

    def request(self, path, payload=None, *, csrf=True, host=None):
        """Send one actual local HTTP request to exercise routing and origin controls.

        Args:
            self: Test instance owning the isolated server, workers and assertions.
            path: Application endpoint or deliberately invalid path.
            payload: Optional JSON mutation body; None sends GET.
            csrf: Whether to include the correct same-origin mutation token.
            host: Optional deliberately hostile Host value.
        Returns:
            Status, decoded JSON or text and headers; the socket is always closed.
        """
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        headers = {}
        if host:
            headers['Host'] = host
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            if csrf:
                headers['X-Clarity-CSRF'] = self.server.app.csrf
        try:
            connection.request('GET' if payload is None else 'POST', path,
                None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            raw = response.read().decode()
            return response.status, json.loads(raw) if 'application/json' in response.getheader('Content-Type') else raw, dict(response.getheaders())
        finally:
            connection.close()

    def launch(self, question='Inspect Alice and Lab X', calls=8):
        """Start a bounded local-document investigation through the public HTTP endpoint.

        Args:
            self: Test server owner.
            question: User question, optionally requesting the fixture clarification.
            calls: Shared model-attempt budget for all production nodes.
        Returns:
            Accepted job receipt; all initial inputs are new to this run.
        """
        status, job, _ = self.request('/api/jobs', dict(question=question, max_requests=calls,
            max_seconds=60, search=False, follow_links=False, coverage_study=False,
            files=[dict(name='example.txt', content=base64.b64encode(SOURCE_TEXT.encode()).decode())]))
        self.assertEqual(status, 202, job)
        return job

    def wait_view(self, job, predicate):
        """Wait for an observed state transition, never infer success from elapsed time.

        Args:
            self: Test instance owning the isolated server, workers and assertions.
            job: Launch receipt whose real run ID is resolved from production events.
            predicate: Condition on the latest read-only dashboard projection.
        Returns:
            Matching view within fifteen seconds; otherwise fails with job receipts.
        """
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            jobs = self.server.app.jobs()
            current = next(j for j in jobs if j['id'] == job['id'])
            if current.get('run_id'):
                view = snapshot(self.root, current['run_id'])
                if predicate(view):
                    return view
            time.sleep(0.05)
        self.fail(f'Expected state was not observed: {jobs}')

    def test_designed_report_launch_uses_production_worker(self):
        """Launch report design through the same endpoint used by the dashboard form.

        Args:
            self: Local server with only the worker's network transport substituted.
        Returns:
            None; verifies design is charged, saved, passed through the graph and
            exposed in both the dashboard snapshot and downloadable report.
        """
        status, job, _ = self.request('/api/jobs', dict(question='Target inventory',
            max_requests=8, max_seconds=300, explore_depth=1, design_answer=True,
            search=False, follow_links=False, coverage_study=False, automatic_stopping=True,
            files=[dict(name='example.txt', content=base64.b64encode(SOURCE_TEXT.encode()).decode())]))
        self.assertEqual(status, 202, job)
        view = self.wait_view(job, lambda v: v['status']['finished'])
        self.assertEqual(view['tokens']['calls'], 5)
        self.assertTrue(view['completion']['ready'])
        self.assertTrue(view['completion']['contract_owned'])
        self.assertEqual(view['completion']['passed'], 1)
        self.assertEqual(view['targets'][0]['supported'], 1)
        self.assertEqual(view['answer_schema']['sections'][0]['fields'][0]['value_kind'], 'person_name')
        status, report, _ = self.request(f"/api/runs/{view['id']}/report")
        self.assertEqual(status, 200)
        self.assertIn('| name | institution | Status | Evidence |', report)

    def test_production_findings_tokens_history_and_evidence_invalidation(self):
        """Verify data from real extraction/review nodes without promoting goal eligibility.

        Args:
            self: Local HTTP and synthetic-provider harness.
        Returns:
            None. Checks live state, final usage, durable gain, read-only projections,
            evidence invalidation and downloadable answer through real endpoints.
        """
        job = self.launch()
        live = self.wait_view(job, lambda v: v['tokens']['calls'] >= 1 and v['status']['worker_running'])
        self.assertFalse(live['status']['finished'])
        view = self.wait_view(job, lambda v: v['status']['finished'])
        self.assertEqual(view['tokens']['calls'], 3)
        self.assertEqual(view['tokens']['total'], 1110)
        self.assertTrue(view['tokens']['complete'])
        self.assertEqual(view['metrics']['supported_claims'], 1)
        self.assertIsNone(view['metrics']['goal_verified_entities'])
        alice = next(e for e in view['entities'] if e['name'] == 'Alice')
        self.assertEqual(alice['supported_claims'], 1)
        self.assertEqual(alice['goal_verification'], 'not_assessed')
        self.assertTrue(any(h['values']['entity_labels'] == 0 for h in view['history']))
        self.assertTrue(any(h['values']['entity_labels'] > 0 and h['values']['supported_claims'] == 0 for h in view['history']))
        directory = self.root / view['id']
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
        status, report, _ = self.request(f"/api/runs/{view['id']}/report")
        self.assertEqual(status, 200, report)
        self.assertIn('Alice', report)
        snapshot(self.root, view['id'])
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()})
        (directory / 'documents/S1/text.txt').write_text('Changed evidence.')
        invalid = snapshot(self.root, view['id'])
        self.assertEqual(invalid['metrics']['supported_claims'], 0)
        self.assertEqual(invalid['claims'][0]['status'], 'invalid_evidence')

    def test_targets_through_production_worker_history_and_source_checks(self):
        """Expose shaped findings without counting the researcher's organization as a person.

        Args:
            self: Real HTTP/worker harness with synthetic transport responses.
        Returns:
            None. Assert run-local target list, graph samples, claim links and source
            invalidation through the production engine; this is not live-model accuracy.
        """
        view = self.wait_view(self.launch('Target inventory'), lambda v: v['status']['finished'])
        goal = view['targets'][0]
        self.assertEqual((goal['identified'], goal['supported']), (1, 1))
        self.assertEqual([r['label'] for r in goal['rows']], ['Alice'])
        self.assertTrue(goal['rows'][0]['candidate_ids'])
        values = [r['values'].get('targets', {}).get('G1', {}) for r in view['history']]
        self.assertTrue(any(v.get('identified') == 1 and v.get('supported') == 0 for v in values))
        self.assertEqual(values[-1]['supported'], 1)
        (self.root / view['id'] / 'documents/S1/text.txt').write_text('Changed evidence.')
        checked = snapshot(self.root, view['id'])
        self.assertEqual(checked['targets'][0]['identified'], 0)
        self.assertEqual(checked['metrics']['targets']['G1']['supported'], 0)

    def test_clarification_continues_same_run_without_replanning(self):
        """Exercise a human answer through the existing checkpoint and review entry point.

        Args:
            self: Isolated synthetic provider harness.
        Returns:
            None. Checks pending questions, accepted continuation and one planner call.
        """
        view = self.wait_view(self.launch('Please clarify the scope'), lambda v: v['status']['waiting_for_input'])
        status, job, _ = self.request(f"/api/runs/{view['id']}/review", dict(
            review_id=view['pending_review']['review_id'], answers={'Q1': 'Include Alice and her lab.'}))
        self.assertEqual(status, 202, job)
        final = self.wait_view(job, lambda v: v['status']['finished'])
        self.assertEqual(final['id'], view['id'])
        self.assertEqual(final['tokens']['by_task']['plan_research']['calls'], 1)
        self.assertEqual(final['metrics']['supported_claims'], 1)
        status, _, _ = self.request(f"/api/runs/{view['id']}/review", dict(review_id='stale', answers={}))
        self.assertEqual(status, 400)

    def test_stop_is_a_cooperative_production_request(self):
        """Stop a running worker through its real cancellation marker.

        Args:
            self: Harness with a deliberately delayed synthetic planner.
        Returns:
            None. Checks the request is acknowledged and no extraction is started.
        """
        job = self.launch()
        view = self.wait_view(job, lambda v: v['status']['worker_running'])
        status, _, _ = self.request(f"/api/runs/{view['id']}/stop", {})
        self.assertEqual(status, 200)
        final = self.wait_view(job, lambda v: v['status']['finished'])
        self.assertLessEqual(final['tokens']['calls'], 1)
        self.assertEqual(final['metrics']['candidate_claims'], 0)

    def test_fresh_runs_do_not_receive_earlier_candidates(self):
        """Distinguish selecting old runs for display from passing their findings to new work.

        Args:
            self: Harness whose output root intentionally contains a finished run.
        Returns:
            None. The new no-evidence response has a different ID and no earlier entities.
        """
        first = self.wait_view(self.launch(), lambda v: v['status']['finished'])
        second = self.wait_view(self.launch('A different empty investigation'), lambda v: v['status']['finished'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(second['entities'], [])
        self.assertEqual(second['claims'], [])
        self.assertEqual(second['tokens']['calls'], 2)

    def test_http_boundaries_and_invalid_inputs_never_start_workers(self):
        """Reject cross-origin mutations, hostile Host names and arbitrary file paths.

        Args:
            self: Empty server with no research in progress.
        Returns:
            None. Valid UI assets remain available and no invalid request launches work.
        """
        self.assertEqual(self.request('/api/runs', host='attacker.invalid')[0], 403)
        self.assertEqual(self.request('/api/jobs', {'question': 'No'}, csrf=False)[0], 403)
        for path in ['/../.env', '/api/runs/../../.env', '/session.json']:
            self.assertEqual(self.request(path)[0], 404)
        for payload in [{'question': 'No', 'files': [False]}, {'question': 'No', 'search': 'false'},
                        {'question': 'No', 'model_concurrency': 0}, {'question': 'No', 'model_concurrency': 17},
                        {'question': 'No', 'model_concurrency': True}, {'question': 'No', 'max_requests': True}, {'question': 'No', 'urls': 'file:///etc/passwd'}]:
            self.assertEqual(self.request('/api/jobs', payload)[0], 400)
        self.assertEqual(self.server.app.processes, {})
        status, _, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])


class TokenReceiptTests(unittest.TestCase):
    """Check telemetry timing and incomplete legacy receipts without a provider call."""

    def test_new_receipts_win_over_lagging_aggregate(self):
        """Keep tokens visible between response persistence and the caller's usage update.

        Args:
            self: Pure data test owner.
        Returns:
            None. Tests real persistence-order edge cases including a failed retry.
        """
        data = dict(request={'budget': {'max_requests': 4}}, usage=dict(requests_attempted=1,
            input_tokens=0, output_tokens=0, total_tokens=0), model_attempts=[dict(
                call_id='plan', task='plan_research', status='response_received',
                token_usage=dict(input_tokens=10, output_tokens=20, total_tokens=30))])
        self.assertEqual(token_usage(data)['total'], 30)
        self.assertTrue(token_usage(data)['complete'])
        failed = copy.deepcopy(data)
        failed['usage'].update(input_tokens=10, output_tokens=20, total_tokens=30)
        failed['model_attempts'].append(dict(call_id='extract', task='extract_knowledge', status='failed'))
        value = token_usage(failed)
        self.assertEqual(value['total'], 30)
        self.assertFalse(value['complete'])
        self.assertEqual(value['missing_calls'], 1)
        active = copy.deepcopy(data)
        active['model_attempts'].append(dict(call_id='extract', task='extract_knowledge', status='started'))
        self.assertFalse(token_usage(active)['complete'])

    def test_legacy_unknown_usage_is_a_subtotal_not_zero(self):
        """Preserve a known extraction receipt when planner or failure usage is absent.

        Args:
            self: Pure data test owner.
        Returns:
            None. One successful receipt is counted once despite a failed retry.
        """
        data = dict(request={'budget': {'max_requests': 4}}, usage=dict(requests_attempted=3,
            input_tokens=None, output_tokens=None, total_tokens=None), model_attempts=[
                dict(call_id='plan', task='plan_research', status='response_received'),
                dict(call_id='S1_P1', task='extract_knowledge', status='failed'),
                dict(call_id='S1_P1', task='extract_knowledge', status='response_received')],
            extraction={'attempts': [dict(batch_id='S1_P1', input_tokens=10, output_tokens=20, total_tokens=30)]})
        value = token_usage(data)
        self.assertEqual(value['total'], 30)
        self.assertEqual(value['known_calls'], 1)
        self.assertEqual(value['missing_calls'], 2)
        self.assertFalse(value['complete'])
