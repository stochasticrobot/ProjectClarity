"""Verify explicit multi-engine recovery at adapters, journals and the production CLI."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from projectclarity.discovery.search_engines import search_readiness
from projectclarity.discovery.search_execution import execute_search
from projectclarity.discovery.search_provider import SearchProviderError
from projectclarity.discovery.search_transport import classify_response
from projectclarity.discovery.wikipedia_search import search_wikipedia
from projectclarity.research_schemas import ResearchRequest, ResearchSession
from projectclarity.nodes.schedule_work.workflow_control import schedule_work
from tests.research.research_test_fixtures import example_session
from tests.research import test_workflow_control_cli as cli_fixtures


class FreeSearchRecoveryTests(unittest.TestCase):
    """Keep provider attempts, delayed work and empty-result semantics inspectable."""

    def session(self, **overrides):
        """Build a small multi-engine request without credentials or live networking.

        Args:
            self: Test instance owning assertions.
            **overrides: Explicit request overrides for a boundary case.

        Returns:
            Mutable namespace with typed request and empty search ledger.
        """
        options = dict(question='Compare research', search_provider='google',
            search_fallbacks=['yahoo', 'wikipedia'], search_cooldown_seconds=300,
            search_recovery_rounds=2, search_min_interval_seconds=0)
        options.update(overrides)
        return SimpleNamespace(request=ResearchRequest(**options), search={})

    def test_failed_engine_falls_through_and_is_skipped_until_cooldown(self):
        """A blocked primary cannot suppress valid results from an enabled peer.

        Args:
            self: Offline adapter test instance.

        Returns:
            None. Assert engine order, retained failure, successful attribution and
            no second request to the cooling provider on the next query.
        """
        session = self.session()
        calls = []

        def search(query, limit, monitor, *, provider):
            """Return a Yahoo lead and reproduce Google's blocked transport.

            Args:
                query: Actual query retained unchanged.
                limit: Requested result cap, sufficient for one lead.
                monitor: Unused optional observer.
                provider: Explicit selected engine.

            Returns:
                One Yahoo lead; Google raises the classified block.
            """
            calls.append(provider)
            if provider == 'google':
                raise SearchProviderError('search_provider_blocked', {'http_status': 403})
            return [{'url': 'https://example.org/lead'}]

        first = {'query': 'broad research'}
        with patch('time.time', return_value=1000):
            execute_search(session, first, search)
            execute_search(session, {'query': 'another topic'}, search)
        self.assertEqual(calls, ['google', 'yahoo', 'yahoo'])
        self.assertEqual(first['provider'], 'yahoo')
        self.assertEqual([a['status'] for a in first['attempts']], ['failed', 'completed'])
        self.assertEqual(session.search['engines']['google']['ready_at'], 1300)

    def test_retry_after_cooldown_restart_and_lifetime_bound(self):
        """Cooldown and attempts survive serialization and eventually stop repeated failure.

        Args:
            self: Test using deterministic time and a failing single engine.

        Returns:
            None. Assert Retry-After dominates five minutes, no early call occurs,
            and three failed rounds exhaust the engine without resetting history.
        """
        session = self.session(search_fallbacks=[], search_retries=0)
        receipt = {'query': 'broad query'}
        error = SearchProviderError('search_rate_limited', {'retry_after_seconds': 900})
        with patch('time.time', return_value=1000), patch('builtins.print'):
            with self.assertRaises(SearchProviderError):
                execute_search(session, receipt, lambda *a, **k: (_ for _ in ()).throw(error))
        session.search = json.loads(json.dumps(session.search))
        self.assertEqual(search_readiness(session), (1900, None))
        with patch('time.time', return_value=1299), patch('projectclarity.discovery.search_provider.search_web') as call:
            with self.assertRaisesRegex(SearchProviderError, 'search_cooldown'):
                execute_search(session, receipt, call)
            call.assert_not_called()
        for now in [1900, 2800]:
            with patch('time.time', return_value=now), self.assertRaises(SearchProviderError):
                execute_search(session, receipt, lambda *a, **k: (_ for _ in ()).throw(error))
        self.assertEqual(len(receipt['attempts']), 3)
        self.assertEqual(search_readiness(session)[1], 'search_engines_exhausted')

    def test_scheduler_keeps_page_work_available_during_search_cooldown(self):
        """Delayed search is not a global workflow block.

        Args:
            self: Journal test instance with an unread supplied page.

        Returns:
            None. Assert the actual scheduler reserves retrieval and restart retains
            the provider's absolute cooldown.
        """
        session = example_session(plan_only=False)
        session.request.budget = session.request.budget.model_copy(update={'max_requests': 10})
        session.request.web_search = True
        session.request.search_cooldown_seconds = 300
        session.request.search_recovery_rounds = 2
        session.search = {'engines': {'duckduckgo': {'ready_at': 1300, 'failures': 1}}}
        with tempfile.TemporaryDirectory() as tmp, patch('time.time', return_value=1000):
            root = Path(tmp)
            (root / session.session_id).mkdir()
            schedule_work({'session': session}, root)
            restored = ResearchSession.model_validate_json((root / session.session_id / 'session.json').read_text())
        self.assertEqual(restored.workflow['next'], 'retrieve_sources')
        self.assertEqual(search_readiness(restored), (1300, None))

    def test_cooldown_beyond_run_time_is_not_shortened(self):
        """The scheduler must stop honestly when recovery cannot fit the run budget.

        Args:
            self: Test using a search-only session and long provider cooldown.

        Returns:
            None. Assert no wait or search and a retained budget-deferral reason.
        """
        session = example_session(plan_only=False)
        session.sources = []
        session.request.budget = session.request.budget.model_copy(update={'max_requests': 10})
        session.request.web_search = True
        session.search = {'engines': {'duckduckgo': {'ready_at': 10000}}}
        with tempfile.TemporaryDirectory() as tmp, patch('time.time', return_value=1000):
            root = Path(tmp)
            (root / session.session_id).mkdir()
            schedule_work({'session': session}, root)
        self.assertEqual(session.workflow['next'], 'stop')
        self.assertIn('retry_after_exceeds_time_budget', session.workflow['stop_details'])

    def test_cancel_and_budget_stop_before_fallback(self):
        """Fallback cannot bypass cancellation or an exhausted byte budget.

        Args:
            self: Adapter test instance.

        Returns:
            None. Assert no transport request after either control boundary.
        """
        session = self.session()
        web = SimpleNamespace(budget=SimpleNamespace(started=0, http_requests=0, bytes_read=10,
            limits=SimpleNamespace(max_run_seconds=1800, max_http_requests=50, max_total_bytes=10)))
        with patch('time.monotonic', return_value=1), patch('projectclarity.discovery.search_provider.search_web') as call:
            with self.assertRaisesRegex(SearchProviderError, 'fetch_budget_exhausted'):
                execute_search(session, {'query': 'topic'}, call, web=web)
            call.assert_not_called()
        monitor = SimpleNamespace(check_cancel=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        with patch('projectclarity.discovery.search_provider.search_web') as call:
            with self.assertRaises(KeyboardInterrupt):
                execute_search(session, {'query': 'topic'}, call, monitor=monitor)
            call.assert_not_called()

    def test_wikipedia_fulltext_is_bounded_and_empty_is_not_a_failure(self):
        """Use documented API search, never the DDGS title-only/body-fetch adapter.

        Args:
            self: API test replacing only HTTP responses.

        Returns:
            None. Assert full-text parameters, normalized snippets, no article
            fetch, explicit empty success and Retry-After preservation.
        """
        from unittest.mock import MagicMock
        http = MagicMock()
        http.__enter__.return_value = http
        response = http.request.return_value
        response.status = 200
        response.headers = {}
        response.read.return_value = json.dumps({'query': {'search': [{'title': 'AI safety',
            'snippet': '<span>AI</span> research'}]}}).encode()
        with patch('projectclarity.discovery.wikipedia_search.urllib3.PoolManager', return_value=http):
            results = search_wikipedia('AI research Canada', 3)
            self.assertEqual(results[0]['url'], 'https://en.wikipedia.org/wiki/AI_safety')
            self.assertEqual(results[0]['snippet'], 'AI research')
            self.assertEqual(http.request.call_args.kwargs['fields']['list'], 'search')
            self.assertFalse(http.request.call_args.kwargs['redirect'])
            self.assertEqual(http.request.call_count, 1)
            response.read.return_value = b'{"query":{"search":[]}}'
            self.assertEqual(search_wikipedia('no matches', 3), [])
        self.assertEqual(classify_response(429, '', {'Retry-After': '900'})['retry_after_seconds'], 900)


class SearchFallbackCLITests(unittest.TestCase):
    """Exercise production CLI selection, acquisition, cooldown and recovery."""

    def test_failed_primary_successful_fallback_reaches_real_acquisition(self):
        """A fallback result must become a retrieved source, not just adapter output.

        Args:
            self: Integration test using synthetic external transports only.

        Returns:
            None. Assert fallback order, final source retrieval, and provider receipts
            through the actual __main__/LangGraph entry point.
        """
        fixture = cli_fixtures.WorkflowControlCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        calls = []

        def search(query, limit, monitor=None, *, provider='duckduckgo'):
            """Block Google and provide an independent Yahoo source.

            Args:
                query: Scheduler-proposed query.
                limit: Result cap, sufficient for the single lead.
                monitor: Optional production observer.
                provider: Explicit selected provider, never an automatic backend.

            Returns:
                A Yahoo lead; Google raises a classified HTTP403 failure.
            """
            calls.append(provider)
            if provider == 'google':
                raise SearchProviderError('search_provider_blocked', {'http_status': 403})
            return [{'url': 'https://example.org/other', 'title': 'Independent lead', 'snippet': ''}]

        fixture.search = search
        _, directory, saved, _ = fixture.run_workflow('--search', '--search-provider', 'google',
            '--search-fallbacks', 'yahoo', '--search-cooldown-seconds', '300', '--search-recovery-rounds', '2')
        self.assertEqual(calls[:2], ['google', 'yahoo'])
        self.assertEqual(calls.count('google'), 1)
        self.assertTrue(any(d['status'] == 'retrieved' and d['original_location'].endswith('/other') for d in saved['documents']))
        first = saved['search']['queries'][0]
        self.assertEqual([a['status'] for a in first['attempts']], ['failed', 'completed'])
        self.assertEqual(first['provider'], 'yahoo')
        self.assertTrue(all(r['provider'] == 'yahoo' for r in saved['search']['results']))
        self.assertIn('search_engine_finished', (directory / 'events.jsonl').read_text())

    def test_all_cooling_retrieves_pages_then_recovers_same_query_through_cli(self):
        """Five-minute recovery must run through the scheduler, not just a helper.

        Args:
            self: Production-entry test with virtual wall time and synthetic transports.

        Returns:
            None. Assert independent retrieval precedes the delayed retry, one logical
            query retains both rounds, and the successful result is fetched.
        """
        fixture = cli_fixtures.WorkflowControlCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cli.plan['strategy'][0]['proposed_searches'] = ['broad discovery']
        # The new timing gate finishes the first two levels before search. Give
        # it a third-hop page so independent work actually remains during cooldown.
        fixture.routes[('example.org', '/first')] = (200, {'Content-Type': 'text/html'},
            b'<main>First page.<a href="/level2">Deeper lead</a></main>')
        fixture.routes[('example.org', '/level2')] = (200, {'Content-Type': 'text/html'},
            b'<main>Second level.<a href="/level3">Further lead</a></main>')
        fixture.routes[('example.org', '/level3')] = (200, {'Content-Type': 'text/plain'}, b'Third level.')
        clock = [1_800_000_000.0]
        calls = []

        def search(query, limit, monitor=None, *, provider='duckduckgo'):
            """Recover only after the real scheduler's virtual cooldown has elapsed.

            Args:
                query: Unchanged logical query.
                limit: Result cap respected by the single synthetic lead.
                monitor: Optional production observer.
                provider: Explicit selected backend.

            Returns:
                Classified temporary block before five minutes; then one new lead.
            """
            calls.append((provider, clock[0], len(fixture.http.requests)))
            if clock[0] < 1_800_000_300:
                raise SearchProviderError('search_provider_blocked', {'http_status': 403})
            return [{'url': 'https://example.org/other', 'title': 'Recovered lead', 'snippet': ''}]

        def advance(seconds):
            """Advance virtual wall time at the sleep boundary without changing routing.

            Args:
                seconds: Actual requested sleep interval.

            Returns:
                None. Increment deterministic time; no real five-minute test delay.
            """
            clock[0] += seconds

        fixture.search = search
        with patch('time.time', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=advance):
            _, _, saved, _ = fixture.run_workflow('--search', '--search-provider', 'google',
                '--search-fallbacks', 'yahoo', '--search-cooldown-seconds', '300',
                '--search-recovery-rounds', '2', '--max-fetch-seconds', '1800', '--max-search-queries', '1')
        self.assertEqual([p for p, _, _ in calls], ['google', 'yahoo', 'google'])
        self.assertGreaterEqual(calls[-1][1] - calls[0][1], 300)
        self.assertGreater(calls[-1][2], calls[0][2])
        self.assertEqual(len(saved['search']['queries']), 1)
        query = saved['search']['queries'][0]
        self.assertEqual((query['status'], query['rounds']), ('completed', 2))
        self.assertEqual(len(query['attempts']), 3)
        self.assertTrue(any(d['original_location'].endswith('/other') and d['status'] == 'retrieved' for d in saved['documents']))

    def test_cli_restart_retains_cooldown_and_does_not_repeat_planning(self):
        """An interrupted cooldown must remain deferred across the public resume command.

        Args:
            self: CLI integration case using virtual wall time and synthetic transports.

        Returns:
            None. Assert the saved deadline survives restart, no immediate provider
            replay occurs, planning is not repeated, and the original query recovers.
        """
        fixture = cli_fixtures.WorkflowControlCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cli.plan['strategy'][0]['proposed_searches'] = ['broad discovery']
        clock = [1_800_000_000.0]
        calls, paused = [], [False]

        def search(query, limit, monitor=None, *, provider='duckduckgo'):
            """Fail until the persisted five-minute recovery time is reached.

            Args:
                query: Original logical query from the resumed scheduler.
                limit: Requested maximum leads.
                monitor: Optional run observer.
                provider: Explicit selected backend.

            Returns:
                One lead after cooldown; otherwise a classified HTTP403 failure.
            """
            calls.append((provider, clock[0]))
            if clock[0] < 1_800_000_300:
                raise SearchProviderError('search_provider_blocked', {'http_status': 403})
            return [{'url': 'https://example.org/other', 'title': 'Recovered', 'snippet': ''}]

        def advance(seconds):
            """Interrupt the first scheduler wait, then advance time during resume.

            Args:
                seconds: Requested sleep duration at the actual runtime boundary.

            Returns:
                None after advancing time; once raises SystemExit to reproduce an
                interrupted worker with a persisted, deferred search receipt.
            """
            if not paused[0]:
                _, saved = fixture.cli.saved_session()
                if saved.get('workflow', {}).get('next') == 'wait':
                    paused[0] = True
                    raise SystemExit(77)
            clock[0] += seconds

        fixture.search = search
        with patch('time.time', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=advance):
            code, _, saved, _ = fixture.run_workflow('--search', '--search-provider', 'google',
                '--search-fallbacks', 'yahoo', '--search-cooldown-seconds', '300',
                '--search-recovery-rounds', '2', '--max-fetch-seconds', '1800', '--max-search-queries', '1')
            self.assertEqual(code, 77)
            self.assertEqual(saved['search']['queries'][0]['status'], 'deferred')
            deadline = calls[0][1] + 300  # Page exploration now precedes the first engine attempt.
            self.assertEqual(saved['search']['engines']['google']['ready_at'], deadline)
            before = len(calls)
            with patch('projectclarity.nodes.web_search.node.search_web', side_effect=search):
                _, resumed = fixture.resume(saved['session_id'])
        self.assertEqual(before, 2)
        self.assertEqual(len(calls), 3)
        self.assertGreaterEqual(calls[-1][1], deadline)
        self.assertEqual(resumed['search']['queries'][0]['status'], 'completed')
        self.assertEqual(sum(t[0] == 'ResearchPlan' for t in fixture.tasks), 1)

    def test_all_engines_failing_stop_at_recovery_limit(self):
        """Persistent failure cannot create infinite deferred query tasks.

        Args:
            self: CLI case with two unavailable engines and one logical query.

        Returns:
            None. Assert three rounds per engine, retained six failures, terminal
            exhaustion and successful independent source retrieval.
        """
        fixture = cli_fixtures.WorkflowControlCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cli.plan['strategy'][0]['proposed_searches'] = ['broad discovery']
        clock, calls = [1_800_000_000.0], []

        def search(query, limit, monitor=None, *, provider='duckduckgo'):
            """Record a permanent outage without changing the graph's decisions.

            Args:
                query: Actual planned query.
                limit: Requested result ceiling.
                monitor: Optional observer.
                provider: Explicit engine identity.

            Returns:
                Never returns; raises a transport failure on every attempt.
            """
            calls.append(provider)
            raise SearchProviderError('search_provider_blocked', {'http_status': 403})

        def advance(seconds):
            """Advance virtual wall time during real scheduler-requested waits.

            Args:
                seconds: Sleep interval requested by the production runtime.

            Returns:
                None. Advance the deterministic test clock.
            """
            clock[0] += seconds

        fixture.search = search
        with patch('time.time', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=advance):
            _, _, saved, _ = fixture.run_workflow('--search', '--search-provider', 'google',
                '--search-fallbacks', 'yahoo', '--search-cooldown-seconds', '300',
                '--search-recovery-rounds', '2', '--max-fetch-seconds', '1800', '--max-search-queries', '1')
        self.assertEqual(calls, ['google', 'yahoo'] * 3)
        self.assertEqual(saved['search']['queries'][0]['status'], 'failed')
        self.assertEqual(len(saved['search']['queries'][0]['attempts']), 6)
        self.assertTrue(all(e['exhausted'] for e in saved['search']['engines'].values()))
        self.assertEqual(saved['workflow']['stop_reason'], 'blocked')
        self.assertTrue(any(d['status'] == 'retrieved' for d in saved['documents']))
