"""Run adversarial workflows through __main__, LangGraph and real saved journals.

Only model/search/HTTP/DNS transports are synthetic. Tests exercise production
selectors, budgets, parsing, evidence persistence, retries and CLI restart. They
do not establish live search availability or the quality of model judgments.
"""
import contextlib
import io
import json
import runpy
import unittest
from collections import Counter
from unittest.mock import patch

import httpx2

from projectclarity.discovery.search_provider import SearchProviderError
from tests.planning import test_planning_cli_integration as planning
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns


class SequenceRoutes(dict):
    """Serve failures followed by success at the source-transport boundary."""

    def __getitem__(self, key):
        """Consume only the first response of a sequence, retaining the final response.

        Args:
            self: Route table containing ordinary responses or response lists.
            key: Requested hostname/path pair from the real fetcher's HTTP call.

        Returns:
            One status/headers/body tuple; unknown destinations remain test errors.
        """
        value = super().__getitem__(key)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value


class WorkflowControlCLITests(unittest.TestCase):
    """Verify combined faults and restart against the deployed application path."""

    def setUp(self):
        """Prepare two source branches and transport switches under an isolated CLI root.

        Args:
            self: Test owner with temporary source inputs and no live credentials.

        Returns:
            None. Install only a source connection-pool fixture; all adapters run.
        """
        self.cli = planning.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan['clarifications'] = []
        self.cli.plan['strategy'][0]['proposed_searches'] = ['initial research', 'next research', 'unattempted research']
        self.routes = SequenceRoutes({
            ('example.org', '/robots.txt'): (200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n'),
            ('example.org', '/research'): (200, {'Content-Type': 'text/html'},
                b'<main>Independent source.<a href="/first">First</a><a href="/second">Second</a></main>'),
            ('example.org', '/first'): (200, {'Content-Type': 'text/plain'}, b'First independent evidence.'),
            ('example.org', '/second'): (200, {'Content-Type': 'text/plain'}, b'Second independent evidence.'),
            ('example.org', '/other'): (200, {'Content-Type': 'text/plain'}, b'Additional seed evidence.'),
        })
        self.http = FakeHTTP(self.routes)
        pool = patch('projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool', side_effect=self.http.pool)
        pool.start()
        self.addCleanup(pool.stop)
        self.tasks = []
        self.link_fault = None
        self.coverage_fault = False
        self.extract_fault = False
        self.crash = False
        self.queries = []

    def provider(self, request):
        """Inject malformed replies and abrupt exits through real SDK requests.

        Args:
            self: Fixture recording actual model schema and passage identities.
            request: Serialized HTTP request produced by the production SDK.

        Returns:
            A provider-shaped response or SystemExit for a reserved in-flight
            attempt. All response parsing and reference checks remain real.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        payload = json.loads(body['messages'][0]['content']) if title != 'ResearchPlan' else {}
        self.tasks.append((title, payload.get('source_id'), payload.get('passage', {}).get('passage_id')))
        count = sum(t[0] == title for t in self.tasks)
        if title == 'ResearchPlan':
            return self.cli.response()
        if title == 'ExtractionBatch':
            if self.crash and count == 1:
                raise SystemExit(77)
            if self.extract_fault and count == 1:
                return self.cli.response(text='{')
            answer = {'entities': [], 'claims': []}
        elif title == 'FollowupPlan':
            if self.link_fault == 'malformed' and count == 1:
                return self.cli.response(text='{')
            choices = [{'link_id': link['link_id'], 'goal_ids': ['G1'], 'reason': 'Inspect this observed lead.'}
                for link in payload['links'][:payload['max_selections']]]
            if self.link_fault == 'mixed' and count == 1:
                choices.insert(1, {'link_id': 'S12_L1', 'goal_ids': ['G1'], 'reason': 'Invalid placeholder.'})
            if self.link_fault == 'all_invalid':
                choices = [{'link_id': 'S12_L1', 'goal_ids': ['G1'], 'reason': 'Invalid placeholder.'}]
            answer = {'gaps': ['Investigate further.'], 'selections': choices, 'stop_reason': 'Proposed leads.'}
        elif title == 'SearchSourceSelection':
            answer = {'selections': [{'result_id': r['result_id'], 'goal_ids': ['G1'], 'reason': 'Inspect observed result.'}
                for r in payload['results']]}
        elif title == 'CoverageAssessment':
            if self.coverage_fault and count == 1:
                return self.cli.response(text='{')
            answer = {'assessments': [{'criterion_id': c['criterion_id'], 'judgment': 'insufficient_evidence',
                'items': [], 'target_count': None, 'benchmark_candidate_ids': [], 'evidence_candidate_ids': [],
                'gaps': ['Evidence remains incomplete.'], 'rationale': 'Synthetic failure-control fixture.'}
                for c in payload['criteria']], 'next_searches': []}
            # Use an actual allowed judgment; this fixture tests scheduling, not semantics.
            for assessment in answer['assessments']:
                assessment['judgment'] = 'partial'
        else:
            raise AssertionError(title)
        return self.cli.response(text=json.dumps(answer))

    def search(self, query, max_results, monitor=None):
        """Return one useful lead, then reproduce an explicit search-provider challenge.

        Args:
            self: Fixture retaining query attempts to detect forbidden repeated search.
            query: Exact production-proposed query string.
            max_results: Requested result ceiling, respected by the single result.
            monitor: Production observer, not replaced or manipulated here.

        Returns:
            One result on the first attempt; subsequent calls raise the actual
            adapter's blocked-provider classification, not an empty success.
        """
        self.queries.append(query)
        if len(self.queries) > 1:
            raise SearchProviderError('search_provider_blocked')
        return [{'url': 'https://example.org/other', 'title': 'Other seed', 'snippet': 'A lead, not evidence.'}]

    def run_workflow(self, *flags):
        """Invoke the normal CLI with bounded calls and real source/model parsing.

        Args:
            self: Fixture whose transport responders inject the configured faults.
            *flags: Additional genuine CLI options for this case.

        Returns:
            Exit code, saved run directory, session dictionary and stderr.
        """
        with patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search):
            code, _, err = self.cli.run_cli(self.provider, '--extract', '--review-claims', '--follow-links',
                '--max-requests', '40', *flags, plan_only=False, source_dns=public_dns, details=False)
        directory, saved = self.cli.saved_session()
        return code, directory, saved, err

    def resume(self, run_id):
        """Execute the public resume command under the same network-forbidden fixture.

        Args:
            self: Fixture retaining prior files and counters across CLI invocations.
            run_id: Existing run identifier printed by the initial CLI process.

        Returns:
            Exit code and saved session after reloading the journal; no planner or
            scheduler is mocked. Unexpected actual sockets fail this test.
        """
        output = io.StringIO()
        with (httpx2.Client(transport=httpx2.MockTransport(self.provider)) as client,
              patch('langchain_anthropic.chat_models._get_default_httpx_client', return_value=client),
              patch.dict('os.environ', {'ANTHROPIC_API_KEY': planning.TEST_KEY}),
              patch('socket.socket.connect', side_effect=AssertionError('Unexpected live network')),
              patch('socket.getaddrinfo', side_effect=public_dns),
              patch('sys.argv', ['projectclarity', 'resume', run_id, '--output-dir', str(self.cli.output), '--quiet-progress']),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(output)):
            with self.assertRaises(SystemExit) as result:
                runpy.run_module('projectclarity', run_name='__main__')
        _, saved = self.cli.saved_session()
        return result.exception.code, saved

    def test_combined_challenge_two_504s_and_invented_link_retains_valid_work(self):
        """Reproduce the harmful failure combination through the real CLI.

        Args:
            self: Synthetic transport fixture with no altered control nodes.

        Returns:
            None. Assert both valid URLs are retrieved, no invented URL is fetched,
            each 504 retries once, original receipts survive, and budgets hold.
        """
        self.link_fault = 'mixed'
        for path in ('/first', '/second'):
            self.routes[('example.org', path)] = [(504, {}, b''), self.routes[('example.org', path)]]
        code, directory, saved, _ = self.run_workflow('--search')
        self.assertEqual(code, 1)  # Blocked search and rejected proposal remain visible.
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(len(saved['documents']), 4)
        self.assertTrue(all(d['status'] == 'retrieved' for d in saved['documents']))
        self.assertEqual(Counter(r[1] for r in self.http.requests)['/first'], 2)
        self.assertEqual(Counter(r[1] for r in self.http.requests)['/second'], 2)
        self.assertEqual(sum(t[0] == 'FollowupPlan' for t in self.tasks), 1)
        decisions = [*saved['discovery']['history'], saved['discovery']]
        mixed = next(d for d in decisions if d.get('items'))
        self.assertEqual([i['status'] for i in mixed['items']], ['accepted', 'rejected', 'accepted'])
        self.assertEqual(mixed['items'][1]['link_id'], 'S12_L1')
        failures = [d for d in saved['workflow']['retrieval_attempts'] if d.get('error_code') == 'http_504']
        self.assertEqual(len(failures), 2)
        for failure in failures:
            original = json.loads((directory / 'documents' / failure['source_id'] / 'metadata.json').read_text())
            self.assertEqual(original['http_status'], 504)
        self.assertLess(saved['usage']['requests_attempted'], 40)
        self.assertLessEqual(saved['retrieval_usage']['http_requests'], saved['request']['fetch_limits']['max_http_requests'])
        self.assertNotIn('edge absent', (directory / 'workflow_graph.txt').read_text())

    def test_malformed_selector_recovers_once_without_repeating_extraction(self):
        """A failed reply retains the same offered work and consumes one bounded retry.

        Args:
            self: Fixture returning malformed JSON only for the first selector call.

        Returns:
            None. Assert two selection calls, three retrieved/extracted sources,
            and distinct saved call traces for the failed and successful attempt.
        """
        self.link_fault = 'malformed'
        code, directory, saved, _ = self.run_workflow()
        self.assertEqual(code, 1)  # Historical failed call is retained, not erased.
        self.assertEqual(len(saved['documents']), 3)
        self.assertEqual(sum(t[0] == 'FollowupPlan' for t in self.tasks), 2)
        self.assertEqual(sum(t[0] == 'ExtractionBatch' for t in self.tasks), 3)
        self.assertTrue((directory / 'llm_calls/discovery_hop1.json').is_file())
        self.assertTrue((directory / 'llm_calls/discovery_hop2.json').is_file())

    def test_all_invalid_replies_stop_at_two_attempts_without_invented_fetch(self):
        """Repeated unusable selections neither spin nor authorize guessed URLs.

        Args:
            self: Fixture selecting the same nonexistent ID on every call.

        Returns:
            None. Assert retry ceiling, no new source, and honest blocked status.
        """
        self.link_fault = 'all_invalid'
        code, _, saved, _ = self.run_workflow()
        self.assertEqual(code, 1)
        self.assertEqual(sum(t[0] == 'FollowupPlan' for t in self.tasks), 2)
        self.assertEqual(len(saved['documents']), 1)
        self.assertEqual(saved['workflow']['stop_reason'], 'blocked')

    def test_restart_does_not_replan_rebill_or_repeat_completed_retrieval(self):
        """An abrupt model-boundary interruption leaves independent queued work resumable.

        Args:
            self: Fixture exiting during the first charged extraction request.

        Returns:
            None. Assert no second planner, no repeat seed fetch or uncertain
            extraction, continued acquisition, retained charge and stable second resume.
        """
        self.cli.urls.write_text('https://example.org/research\nhttps://example.org/other\n')
        self.crash = True
        code, _, saved, _ = self.run_workflow()
        self.assertEqual(code, 77)
        before = saved['usage']['requests_attempted']
        self.assertIsNotNone(saved['workflow']['active'])
        code, saved = self.resume(saved['session_id'])
        self.assertEqual(code, 1)
        self.assertGreater(saved['usage']['requests_attempted'], before)
        self.assertEqual(sum(t[0] == 'ResearchPlan' for t in self.tasks), 1)
        self.assertEqual(sum(t[0] == 'ExtractionBatch' and t[1] == 'S1' for t in self.tasks), 1)
        self.assertEqual(sum(r[1] == '/research' for r in self.http.requests), 1)
        self.assertTrue(any(r[1] == '/other' for r in self.http.requests))
        self.assertTrue(any(t['status'] == 'uncertain' for t in saved['workflow']['tasks'].values()))
        calls, requests = len(self.tasks), len(self.http.requests)
        self.resume(saved['session_id'])
        self.assertEqual((len(self.tasks), len(self.http.requests)), (calls, requests))

    def test_malformed_extraction_and_coverage_do_not_stop_other_sources(self):
        """Independent documents survive failed model tasks, not just broken selectors.

        Args:
            self: Fixture with one bad extraction and one bad coverage response.

        Returns:
            None. Assert new links still yield completed passages and retained failures.
        """
        self.extract_fault = self.coverage_fault = True
        code, _, saved, _ = self.run_workflow('--assess-coverage')
        self.assertEqual(code, 1)
        self.assertEqual(len(saved['documents']), 3)
        self.assertEqual(sum(a['status'] == 'completed' for a in saved['extraction']['attempts']), 2)
        self.assertTrue(any(c.get('error_code') == 'invalid_output_schema' for c in saved['processing_calls']))

    def test_long_source_yields_to_discovery_and_other_source_processing(self):
        """Document length cannot drain the model allowance before another lead is explored.

        Args:
            self: Fixture with a many-passage source and a small linked source.

        Returns:
            None. Assert selection and child extraction occur before the long
            source finishes, while all pending passage identities remain saved.
        """
        text = ('A long independent context paragraph.\n' * 8000).encode()
        self.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'},
            b'<main><pre>' + text + b'</pre><a href="/first">Small source</a></main>')
        _, _, saved, _ = self.run_workflow('--max-requests', '12')
        long_positions = [i for i, task in enumerate(self.tasks) if task[0] == 'ExtractionBatch' and task[1] == 'S1']
        selection = next(i for i, task in enumerate(self.tasks) if task[0] == 'FollowupPlan')
        child = next(i for i, task in enumerate(self.tasks) if task[0] == 'ExtractionBatch' and task[1] == 'S2')
        self.assertLess(selection, long_positions[-1])
        self.assertLess(child, long_positions[-1])
        self.assertLessEqual(saved['usage']['requests_attempted'], 12)
        self.assertTrue(any(a['status'] == 'not_attempted' for a in saved['extraction']['attempts']))

    def test_retry_after_beyond_remaining_time_is_deferred_without_busy_wait(self):
        """A server cooldown longer than the run cannot be shortened to force a retry.

        Args:
            self: Source transport fixture returning 503 and a one-hour cooldown.

        Returns:
            None. Assert the failed page is not retried, its peer succeeds, and
            deferred work remains explicit without waiting an hour in the test.
        """
        self.routes[('example.org', '/first')] = (503, {'Retry-After': '3600'}, b'')
        code, _, saved, _ = self.run_workflow()
        self.assertEqual(code, 1)
        self.assertEqual(sum(r[1] == '/first' for r in self.http.requests), 1)
        self.assertTrue(any(d['status'] == 'retrieved' and d['original_location'].endswith('/second')
            for d in saved['documents']))
        self.assertIn('retry_after_exceeds_time_budget', saved['workflow']['stop_details'])

    def test_robots_526_and_missing_page_are_terminal_but_do_not_stop_peers(self):
        """Certificate-related permission failure and 404 cannot stall independent sources.

        Args:
            self: Fixture with three supplied sources under two public hostnames.

        Returns:
            None. Assert denied robots is never bypassed, 404 is not retried, and
            the independent source still reaches extraction.
        """
        self.cli.urls.write_text('https://broken.example/page\nhttps://example.org/missing\nhttps://example.org/other\n')
        self.routes[('broken.example', '/robots.txt')] = (526, {}, b'')
        self.routes[('example.org', '/missing')] = (404, {}, b'')
        code, _, saved, _ = self.run_workflow()
        self.assertEqual(code, 1)
        self.assertEqual(sum(r[1] == '/missing' for r in self.http.requests), 1)
        self.assertFalse(any(r[1] == '/page' for r in self.http.requests))
        self.assertTrue(any(t[0] == 'ExtractionBatch' and t[1] == 'S3' for t in self.tasks))
        self.assertEqual({d['error_code'] for d in saved['documents'] if d['status'] == 'failed'},
            {'robots_unavailable', 'http_status'})
