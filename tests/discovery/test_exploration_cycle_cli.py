"""Prove explore-before-search ordering through the production CLI and graph."""
import json
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from projectclarity.discovery.search_provider import SearchProviderError
from projectclarity.research_schemas import ResearchRequest
from tests.discovery import test_branch_progress_cli as branches


class ExplorationCycleCLITests(unittest.TestCase):
    """Replace external transports only; inspect real journals at search time."""

    def setUp(self):
        """Install isolated HTML/model/search transports and an ordered call ledger.

        Args:
            self: Case owning temporary files and transport fixtures.

        Returns:
            None. No actual sockets or provider credentials are permitted.
        """
        self.branch = branches.BranchProgressCLITests()
        self.branch.setUp()
        self.addCleanup(self.branch.doCleanups)
        self.flow = self.branch.flow
        self.base_provider = self.flow.provider
        self.flow.provider = self.provider
        self.flow.search = self.search
        self.events, self.snapshots = [], []
        self.search_results = []
        self.search_error = False
        self.fresh_queries = True
        self.coverage_failure = False
        self.crash_search = False

    def provider(self, request):
        """Record real serialized model tasks and supply bounded coverage proposals.

        Args:
            self: Fixture with optional malformed coverage and page-specific replies.
            request: Actual provider request created by the production SDK.

        Returns:
            A synthetic SDK response. Extraction/review/selector validation stays real.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        self.events.append(title)
        response = self.base_provider(request)
        if title == 'CoverageAssessment':
            if self.coverage_failure:
                return self.flow.cli.response(text='{')
            answer = json.loads(response.json()['content'][0]['text'])
            if self.fresh_queries:
                number = self.events.count(title)
                answer['next_searches'] = [{'query': f'evidence gap {number}', 'goal_ids': ['G1'],
                    'reason': 'Synthetic gap from the completed exploration wave.'}]
            return self.flow.cli.response(text=json.dumps(answer))
        return response

    def search(self, query, max_results, monitor=None, **kwargs):
        """Snapshot production state precisely when a search engine is invoked.

        Args:
            self: Fixture collecting real search timing and optional failures.
            query: Production-selected query, not chosen by this responder.
            max_results: Engine result allowance.
            monitor: Production monitor left unchanged.
            **kwargs: Optional explicitly selected engine name.

        Returns:
            Configured leads, or a classified engine failure. An optional abrupt
            exit simulates interruption after the real search reservation is saved.
        """
        self.events.append('SEARCH:' + query)
        self.flow.queries.append(query)
        _, saved = self.flow.cli.saved_session()
        self.snapshots.append(saved)
        if self.crash_search:
            self.crash_search = False
            raise SystemExit(77)
        if self.search_error:
            raise SearchProviderError('search_provider_blocked')
        return self.search_results[:max_results]

    def chain(self, names):
        """Install a productive chain so branch pruning does not hide timing bugs.

        Args:
            self: Fixture owning page routes.
            names: Ordered URL path components, starting with the supplied root.

        Returns:
            None. Each source contributes a distinct anchored, reviewed claim.
        """
        for index, name in enumerate(names):
            self.branch.page(name, names[index + 1:index + 2], claim=f'Group {name} researches attacks.')

    def run_case(self, *flags):
        """Execute a fresh production run with default two-level gating.

        Args:
            self: Case with no model/application-node replacements.
            *flags: Extra real CLI arguments overriding fixture resource limits.

        Returns:
            Exit code, run directory, saved session and console output.
        """
        return self.branch.run_case('--search', '--search-fallbacks', 'none',
            '--search-min-interval-seconds', '0', '--search-retries', '0',
            '--search-recovery-rounds', '0', *flags)

    def completed_paths(self, saved):
        """Resolve fully finalized successful analysis from saved passage receipts.

        Args:
            self: Test owner; no mutation occurs.
            saved: Real saved session at a search boundary or run completion.

        Returns:
            Paths with all inventoried extraction passages completed and finalized.
        """
        final = set(saved['workflow'].get('finalized_batches', []))
        result = set()
        for source in saved['sources']:
            rows = [p for p in (saved.get('extraction') or {}).get('attempts', [])
                if p['source_id'] == source['source_id']]
            if rows and all(p['status'] == 'completed' and p['batch_id'] in final for p in rows):
                result.add(urlsplit(source['url']).path)
        return result

    def test_two_levels_analyzed_then_fresh_gap_search_repeats_beyond_depth_two(self):
        """Require two distinct waves, reviews and informed query ordering.

        Args:
            self: Productive chain extending beyond both search checkpoints.

        Returns:
            None. Assert actual search-time receipts, not helper metadata alone.
        """
        self.chain(['research', 'a', 'b', 'c', 'd', 'e'])
        code, _, saved, err = self.run_case()
        self.assertEqual(code, 0, err[-1800:])
        self.assertEqual(self.completed_paths(self.snapshots[0]), {'/research', '/a', '/b'})
        self.assertEqual(self.completed_paths(self.snapshots[2]), {'/research', '/a', '/b', '/c', '/d'})
        self.assertIn('/e', self.completed_paths(saved))
        self.assertEqual(self.flow.queries[0], 'evidence gap 1')
        self.assertEqual(self.flow.queries[2], 'evidence gap 2')
        first = next(i for i, e in enumerate(self.events) if e.startswith('SEARCH:'))
        self.assertEqual(self.events[:first].count('ClaimReviewBatch'), 3)
        self.assertEqual(self.events[:first].count('CoverageAssessment'), 1)
        self.assertEqual(self.snapshots[0]['workflow']['exploration_cycle']['history'][-1]['depths'],
            {'S1': 0, 'S2': 1, 'S3': 2})
        self.assertIn('up to 2 link levels before search', err)

    def test_sibling_batches_finish_the_wave_before_search(self):
        """Do not confuse two selector calls with two analyzed link levels.

        Args:
            self: Two branches and a one-source selection quantum.

        Returns:
            None. Both depth-two children are analyzed before the first query.
        """
        self.branch.page('research', ['a', 'x'])
        self.branch.page('a', ['b'])
        self.branch.page('x', ['y'])
        self.branch.page('b', ['c'])
        self.branch.page('y', ['z'])
        self.branch.page('c')
        self.branch.page('z')
        _, _, _, _ = self.run_case('--max-followup-sources', '1')
        self.assertEqual(self.completed_paths(self.snapshots[0]), {'/research', '/a', '/b', '/x', '/y'})

    def test_long_boundary_page_finishes_all_passages_before_search(self):
        """A useful early passage must not count as fully exploring a long page.

        Args:
            self: Depth-two document with many passages and an early useful claim.

        Returns:
            None. Search sees no pending passages and follows all reviews.
        """
        self.chain(['research', 'a', 'b', 'c'])
        self.branch.page('b', ['c'], claim='Group b researches attacks.',
            prefix='Group b researches attacks.\n' + 'Background context only.\n' * 1100)
        self.run_case()
        snapshot = self.snapshots[0]
        sid = next(s['source_id'] for s in snapshot['sources'] if s['url'].endswith('/b'))
        rows = [p for p in snapshot['extraction']['attempts'] if p['source_id'] == sid]
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(p['status'] == 'completed' for p in rows))
        self.assertIn('/b', self.completed_paths(snapshot))
        self.assertNotIn('/c', self.completed_paths(snapshot))

    def test_empty_and_failed_frontier_search_early_without_claiming_two_levels(self):
        """Search can bootstrap after a source failure without fabricating progress.

        Args:
            self: Supplied root returning an unretryable HTTP failure.

        Returns:
            None. Preserve failure and report the blocked-frontier exception.
        """
        self.branch.page('research')
        self.flow.routes[('example.org', '/research')] = (404, {}, b'')
        self.run_case()
        self.assertTrue(self.snapshots)
        self.assertEqual(self.completed_paths(self.snapshots[0]), set())
        self.assertEqual(self.snapshots[0]['documents'][0]['status'], 'failed')

    def test_search_without_seeds_bootstraps_then_explores_results(self):
        """Existing search results become roots before another engine request.

        Args:
            self: No supplied references; a search result links to two descendants.

        Returns:
            None. Bootstrap search is immediate; the next batch follows analysis.
        """
        self.flow.cli.plan['strategy'][0]['source_ids'] = []
        self.chain(['other', 'a', 'b', 'c'])
        self.search_results = [{'url': 'https://example.org/other', 'title': 'Root', 'snippet': 'Lead'}]
        from tests.retrieval.retrieval_test_fixtures import public_dns
        with (patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search),
              patch('projectclarity.retrieval.retrieval_limits.time.sleep')):
            code, _, err = self.flow.cli.run_cli(self.provider, '--search', '--extract', '--review-claims',
                '--follow-links', '--max-requests', '100', '--search-fallbacks', 'none',
                '--search-min-interval-seconds', '0', '--search-recovery-rounds', '0',
                include_urls=False, plan_only=False, source_dns=public_dns, details=False)
        _, saved = self.flow.cli.saved_session()
        self.assertEqual(code, 0, err[-1800:])
        self.assertEqual(self.completed_paths(self.snapshots[0]), set())
        self.assertEqual(self.completed_paths(self.snapshots[2]), {'/other', '/a', '/b'})
        self.assertIn('/c', self.completed_paths(saved))

    def test_exhausted_search_does_not_cap_deeper_crawling(self):
        """A permanently blocked engine cannot turn a timing rule into a depth cap.

        Args:
            self: Seven-page productive chain and an exhausted engine.

        Returns:
            None. All seven pages reach analysis after the first search fails.
        """
        names = ['research', 'a', 'b', 'c', 'd', 'e', 'f']
        self.chain(names)
        self.search_error = True
        _, _, saved, _ = self.run_case()
        self.assertEqual(self.completed_paths(saved), {'/' + n for n in names})
        self.assertEqual(len(self.flow.queries), 1)
        self.assertTrue(any(h['number'] >= 2 for h in saved['workflow']['exploration_cycle']['history']))

    def test_failed_coverage_is_bounded_and_does_not_deadlock_search(self):
        """Gap assessment failure is explicit and cannot suppress all later search.

        Args:
            self: Three analyzed pages and a persistently malformed coverage reply.

        Returns:
            None. Two bounded assessment attempts precede the first search.
        """
        self.chain(['research', 'a', 'b'])
        self.coverage_failure = True
        _, _, saved, _ = self.run_case()
        self.assertEqual(self.completed_paths(self.snapshots[0]), {'/research', '/a', '/b'})
        first = next(i for i, e in enumerate(self.events) if e.startswith('SEARCH:'))
        self.assertEqual(self.events[:first].count('CoverageAssessment'), 2)
        self.assertTrue(saved['coverage']['history'][-1]['error_code'])

    def test_resume_keeps_relative_depth_and_does_not_replan_or_replay_model(self):
        """Resume an interrupted depth-one analysis with an independent sibling.

        Args:
            self: Saved active model reservation and a sibling reaching depth two.

        Returns:
            None. The real resume command preserves the wave and charges uncertain work.
        """
        self.branch.page('research', ['a', 'x'])
        self.branch.page('a', ['b'])
        self.branch.page('x', ['y'])
        self.branch.page('y', ['z'])
        self.branch.page('z')
        self.branch.crash_on = 'a'
        code, _, before, _ = self.run_case()
        self.assertEqual(code, 77)
        with (patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search),
              patch('projectclarity.retrieval.retrieval_limits.time.sleep')):
            _, after = self.flow.resume(before['session_id'])
        self.assertEqual(self.events.count('ResearchPlan'), 1)
        self.assertEqual(self.completed_paths(self.snapshots[0]), {'/research', '/x', '/y'})
        self.assertTrue(any(t['status'] == 'uncertain' for t in after['workflow']['tasks'].values()))
        self.assertEqual(after['request']['explore_before_search_depth'], 2)
        self.assertGreaterEqual(after['usage']['requests_attempted'], before['usage']['requests_attempted'])

    def test_disabled_and_legacy_requests_keep_original_search_timing(self):
        """An explicit opt-out and old API records do not acquire the new policy.

        Args:
            self: Productive input with a disabled CLI scheduling gate.

        Returns:
            None. Search runs before extraction; old request fields default to None.
        """
        self.chain(['research', 'a', 'b'])
        _, _, saved, _ = self.run_case('--explore-before-search-depth', '0')
        self.assertEqual(self.completed_paths(self.snapshots[0]), set())
        self.assertNotIn('exploration_cycle', saved['workflow'])
        self.assertIsNone(ResearchRequest(question='A legacy request').explore_before_search_depth)

    def test_cooldown_continues_deeper_crawl_before_delayed_search_retry(self):
        """A five-minute search cooldown must not stall a productive third hop.

        Args:
            self: Productive chain, one logical query and virtual transport time.

        Returns:
            None. All pages finish before retry; the engine waits its full cooldown.
        """
        self.chain(['research', 'a', 'b', 'c', 'd', 'e'])
        self.flow.cli.plan['strategy'][0]['proposed_searches'] = ['one broad query']
        clock = [1_800_000_000.0]
        calls = []

        def search(query, maximum, monitor=None):
            """Fail until the configured retry deadline at the engine boundary.

            Args:
                query: Actual selected query text.
                maximum: Result allowance.
                monitor: Unmodified production observer.

            Returns:
                Empty success after two cooldowns, otherwise a classified failure.
            """
            calls.append(clock[0])
            self.search_error = clock[0] < 1_800_000_900
            return self.search(query, maximum, monitor)

        def advance(seconds):
            """Advance a virtual clock only when production requests a sleep.

            Args:
                seconds: Requested wait duration.

            Returns:
                None. Update wall time without changing scheduling decisions.
            """
            clock[0] += seconds

        self.flow.search = search
        with patch('time.time', side_effect=lambda: clock[0]), patch('time.sleep', side_effect=advance):
            _, _, saved, _ = self.flow.run_workflow('--search', '--max-requests', '100',
                '--search-fallbacks', 'none', '--search-min-interval-seconds', '0',
                '--search-cooldown-seconds', '300', '--search-recovery-rounds', '2',
                '--max-fetch-seconds', '1800', '--max-search-queries', '1')
        self.assertEqual(self.completed_paths(self.snapshots[0]), {'/research', '/a', '/b'})
        self.assertEqual(self.completed_paths(self.snapshots[1]), {'/research', '/a', '/b', '/c', '/d', '/e'})
        self.assertGreaterEqual(calls[1] - calls[0], 300)
        self.assertGreaterEqual(calls[2] - calls[1], 600)
        self.assertEqual(len(saved['search']['queries']), 1)
        self.assertEqual(saved['search']['queries'][0]['rounds'], 3)
        self.assertTrue(saved['coverage']['next_searches'])  # Fresh, but no free logical-query slots.

    def test_interrupted_search_resumes_next_wave_without_replaying_query(self):
        """Search reservation commits the next exploration cycle before external I/O.

        Args:
            self: Productive chain with an abrupt exit during the first engine call.

        Returns:
            None. Resume explores two more hops and never replays the uncertain query.
        """
        self.chain(['research', 'a', 'b', 'c', 'd', 'e'])
        self.crash_search = True
        code, _, before, _ = self.run_case()
        self.assertEqual(code, 77)
        first_query = self.flow.queries[0]
        with (patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search),
              patch('projectclarity.retrieval.retrieval_limits.time.sleep')):
            _, after = self.flow.resume(before['session_id'])
        self.assertEqual(self.events.count('ResearchPlan'), 1)
        self.assertEqual(self.flow.queries.count(first_query), 1)
        self.assertEqual(self.completed_paths(self.snapshots[1]), {'/research', '/a', '/b', '/c', '/d'})
        self.assertTrue(any(t['status'] == 'uncertain' and t['action'] == 'web_search'
            for t in after['workflow']['tasks'].values()))

    def test_model_budget_stops_without_bypassing_analysis_gate(self):
        """The timing policy never buys extra calls to force a full exploration wave.

        Args:
            self: Chain requiring more analysis than the explicit request allowance.

        Returns:
            None. Preserve the model cap and explicit unfinished-work accounting.
        """
        self.chain(['research', 'a', 'b', 'c'])
        _, _, saved, _ = self.run_case('--max-requests', '9')
        self.assertLessEqual(saved['usage']['requests_attempted'], 9)
        self.assertEqual(self.flow.queries, [])
        self.assertEqual(saved['workflow']['stop_reason'], 'budget_exhausted')

    def test_operator_one_level_override_changes_actual_search_boundary(self):
        """The documented depth flag controls routing rather than just metadata.

        Args:
            self: Productive chain with an explicit one-hop scheduling window.

        Returns:
            None. The first search follows the first child, not the grandchild.
        """
        self.chain(['research', 'a', 'b', 'c'])
        self.run_case('--explore-before-search-depth', '1')
        self.assertEqual(self.completed_paths(self.snapshots[0]), {'/research', '/a'})
