"""Check that search-provider failures remain distinct from valid empty results.

Controlled HTTP responses and clocks reproduce a useful first query followed by
an access challenge or temporary failure. Component cases exercise the real
search worker and DDGS parsing; a command-line case checks how the workflow retains
previous results and handles the later failure. No real engine is queried and no
challenge-solving behavior is implemented by these tests.
"""
import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from projectclarity.discovery.search_execution import execute_search
from projectclarity.discovery.search_provider import SearchProviderError
from projectclarity.discovery.search_worker import main
from projectclarity.research_schemas import ResearchRequest
from tests.discovery import test_search_coverage_pipeline as search_fixtures


class SearchFailureRegressionTests(unittest.TestCase):
    """Keep the real DDGS parser and worker protocol for the observed HTTP-202 failure."""

    def test_production_graph_keeps_first_results_and_stops_after_second_query_challenge(self):
        """Exercise circuit-breaker routing through the terminal interface, not just a wrapper helper.

        Args:
            self: Offline graph test replacing external search and source/model HTTP.

        Returns:
            None. Assert first-query evidence is processed, the second failure remains
            explicit, later proposed searches remain queued and no third call is made.
        """
        fixture = search_fixtures.SearchCoveragePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cli.plan['strategy'][0]['proposed_searches'].append('second discovery query')

        def blocked_second(query, limit, monitor=None):
            """Return one useful result then the observed provider challenge.

            Args:
                query: Actual query selected by the production scheduler.
                limit: Result allowance forwarded to the normal fixture adapter.
                monitor: Shared run monitor forwarded on the successful attempt.

            Returns:
                First-query fixture results; raises a challenge error on later calls.
            """
            if fixture.search_calls:
                fixture.search_calls.append(query)
                raise SearchProviderError('search_provider_blocked', {'http_status': 202,
                    'response_kind': 'challenge'})
            return fixture.search(query, limit, monitor)

        code, _, err, _, saved = fixture.run_pipeline(search=blocked_second)
        self.assertEqual(code, 1, err)
        self.assertEqual(len(fixture.search_calls), 2)
        self.assertEqual([q['status'] for q in saved['search']['queries']], ['completed', 'failed'])
        self.assertEqual(saved['search']['queries'][1]['attempts'][0]['diagnostics']['http_status'], 202)
        self.assertEqual(saved['search']['stop_reason'], 'search_provider_blocked')
        self.assertEqual(saved['documents'][0]['status'], 'retrieved')
        self.assertTrue(saved['extraction']['candidates'])
        self.assertTrue(saved['coverage']['next_searches'])

    def test_empty_marker_and_unrecognized_html_are_distinct(self):
        """Distinguish a confirmed empty response from HTML the parser cannot understand.

        Args:
            self: Offline worker test preserving the real DDGS engine and parser.

        Returns:
            None. Assert confirmed emptiness succeeds while unknown HTML remains an error.
        """
        for body, expected in [('<div class="no-results">No results</div>', None),
                               ('<html><p>Unexpected response format</p></html>', 'search_response_unrecognized')]:
            output = io.StringIO()
            with self.subTest(expected=expected), patch('ddgs.http_client.HttpClient.request',
                    return_value=SimpleNamespace(status_code=200, text=body)), patch('sys.stdin',
                    io.StringIO(json.dumps({'query': 'fixture query', 'max_results': 5}))), contextlib.redirect_stdout(output):
                code = main()
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0 if expected is None else 1)
            if expected:
                self.assertEqual(result['error']['code'], expected)
            else:
                self.assertEqual(result, [])

    def test_second_search_challenge_is_not_reported_as_no_results(self):
        """Reproduce a success followed by a challenge through the actual worker and DDGS.

        Args:
            self: Offline test case; only the HTTP response boundary is substituted.

        Returns:
            None. Fail if the first results disappear, the second response is called
            empty/generic failure, or HTTP evidence fails to survive classification.
        """
        responses = [SimpleNamespace(status_code=200, text='<html><div class="body"><h2>Research</h2><a href="https://example.org/research">Research lead</a></div></html>'),
            SimpleNamespace(status_code=202, text='<html><form id="challenge-form"><div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div></form></html>')]
        outputs = []
        with patch('ddgs.http_client.HttpClient.request', side_effect=responses) as http:
            for query in ['first discovery query', 'second discovery query']:
                output = io.StringIO()
                with patch('sys.stdin', io.StringIO(json.dumps({'query': query, 'max_results': 5}))), contextlib.redirect_stdout(output):
                    code = main()
                outputs.append((code, json.loads(output.getvalue())))
        self.assertEqual(http.call_count, 2)
        self.assertEqual(outputs[0][0], 0)
        self.assertEqual(outputs[0][1][0]['url'], 'https://example.org/research')
        self.assertEqual(outputs[1][0], 1)
        self.assertEqual(outputs[1][1]['error']['code'], 'search_provider_blocked')
        self.assertEqual(outputs[1][1]['error']['http_status'], 202)
        self.assertEqual(outputs[1][1]['error']['response_kind'], 'challenge')

    def test_consecutive_searches_are_paced_and_transient_second_failure_retries(self):
        """Enforce completion-to-start spacing and retain a successful bounded retry.

        Args:
            self: Offline test using an advancing fake clock and adapter responses.

        Returns:
            None. Fail on missing pacing, incorrect query/result association, omitted
            failure receipts, or retries exceeding the configured allowance.
        """
        session = SimpleNamespace(request=ResearchRequest(search_min_interval_seconds=3.0,
            search_retries=1, question='Compare methods'), search={})
        clock = [100.0]
        starts = []

        def sleep(seconds):
            """Advance simulated time instead of blocking the test.

            Args:
                seconds: Requested wait duration.

            Returns:
                None. Increment the deterministic clock.
            """
            clock[0] += seconds

        def search(query, limit, monitor):
            """Fail the second provider attempt, then recover for the same logical query.

            Args:
                query: Logical query passed unchanged by production scheduling.
                limit: Provider result allowance, unused by this one-result fixture.
                monitor: Optional cancellation monitor, unused here.

            Returns:
                One query-labelled result; raises a transient timeout on attempt two.
            """
            starts.append((query, clock[0]))
            if len(starts) == 2:
                raise SearchProviderError('search_timeout')
            return [{'url': 'https://example.org/' + query}]

        first, second = {'query': 'first'}, {'query': 'second'}
        with patch('projectclarity.discovery.search_execution.time.time', side_effect=lambda: clock[0]), patch(
                'projectclarity.discovery.search_execution.time.sleep', side_effect=sleep):
            a = execute_search(session, first, search)
            b = execute_search(session, second, search)
        self.assertEqual([q for q, _ in starts], ['first', 'second', 'second'])
        self.assertGreaterEqual(starts[1][1] - starts[0][1], 3)
        self.assertGreaterEqual(starts[2][1] - starts[1][1], 6)
        self.assertEqual(a[0]['url'], 'https://example.org/first')
        self.assertEqual(b[0]['url'], 'https://example.org/second')
        self.assertEqual([a['status'] for a in second['attempts']], ['failed', 'completed'])

    def test_challenge_opens_circuit_without_retry(self):
        """Do not attempt to solve or repeatedly request a provider challenge.

        Args:
            self: Offline test case with retries enabled to exercise the exception.

        Returns:
            None. Assert one attempted call and a durable run-local blocked-provider marker.
        """
        session = SimpleNamespace(request=ResearchRequest(question='Compare methods',
            search_min_interval_seconds=0.0, search_retries=3), search={})
        receipt = {'query': 'second'}
        with patch('projectclarity.discovery.search_provider.search_web', side_effect=
                SearchProviderError('search_provider_blocked', {'http_status': 202})) as search:
            with self.assertRaises(SearchProviderError):
                execute_search(session, receipt, search)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(session.search['provider_blocked']['reason'], 'search_provider_blocked')
        self.assertEqual(len(receipt['attempts']), 1)
