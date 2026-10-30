"""Check that one failed research operation does not discard independent queued work.

Synthetic seed-library pages, search results and model replies exercise the real
command-line workflow. Tests combine failures and successful work and inspect
saved tasks, limits and outcomes. The fixtures are chosen to test scheduling and
failure isolation rather than a particular research answer. They do not establish
that a live model finds relevant sources or extracts all useful information.
"""
import copy
import json
import unittest
from unittest.mock import patch

from tests.planning import test_planning_cli_integration as planning_tests
from tests.retrieval.retrieval_test_fixtures import FakeHTTP, public_dns
from tests.extraction import test_knowledge_extraction_integration as extraction_tests
from tests.knowledge import test_knowledge_pipeline as knowledge_tests
from tests.discovery import test_search_coverage_pipeline as search_tests
from tests.extraction.extraction_test_fixtures import proposed_batch
from projectclarity.discovery.search_provider import SearchProviderError


class DiscoveryRecoveryTests(unittest.TestCase):
    """Keep production routing, schemas, quote validation and persistence intact."""

    def setUp(self):
        """Prepare independent search directions and linked documents under synthetic HTTP.

        Args:
            self: Test instance owning temporary storage and transport fixtures.

        Returns:
            None. Register cleanup; no network or research call is made.
        """
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan['clarifications'] = []
        self.cli.plan['objective'] = 'Identify community seed libraries and their collections.'
        self.cli.plan['scope_in'] = ['Seed libraries and collections']
        self.cli.plan['scope_out'] = ['Unsupported claims']
        self.cli.plan['goals'][0]['question'] = 'Which seed libraries have documented collections?'
        self.cli.plan['goals'][1]['question'] = 'What evidence describes those collections?'
        step = self.cli.plan['strategy'][0]
        step.update(source_ids=[], proposed_searches=['directory one', 'directory two', 'directory three'])
        second = copy.deepcopy(step)
        second.update(action='Explore independent collection catalogues.',
            proposed_searches=['catalogue one', 'catalogue two', 'catalogue three'])
        self.cli.plan['strategy'].append(second)
        self.queries = []
        self.tasks = []
        self.payloads = []
        self.fail_search = False
        self.fail_link = False
        self.fail_capture = False
        self.extra_coverage_queries = False
        self.bad_coverage = False
        self.many_results = False
        routes = {('library.example', '/robots.txt'): (200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n')}
        for index in range(1, 25):
            body = f'Library {index} maintains a seed collection.\n' + ('Collection catalogue entry.\n' * 160)
            routes[('library.example', f'/search-{index}')] = (200, {'Content-Type': 'text/html'},
                (f'<main><pre>{body}</pre><a href="/linked-{index}">Collection details</a></main>').encode())
            routes[('library.example', f'/linked-{index}')] = (200, {'Content-Type': 'text/plain'},
                f'Library {index} offers seeds.'.encode())
        self.http = FakeHTTP(routes)

    def search(self, query, max_results, monitor=None):
        """Return independent leads or one deliberate failure without querying a real provider.

        Args:
            self: Fixture storing attempted queries and response switches.
            query: Actual production-selected query, retained to check ordering.
            max_results: Requested provider limit; fixture never exceeds it.
            monitor: Production monitor accepted without overriding its behavior.

        Returns:
            Synthetic result rows; raise SearchProviderError for the configured first attempt.
        """
        self.queries.append(query)
        if self.fail_search and len(self.queries) == 1:
            raise SearchProviderError('search_timeout')
        first = (len(self.queries) - 1) * max_results + 1 if self.many_results else len(self.queries)
        count = max_results if self.many_results else 1
        return [{'url': f'https://library.example/search-{i}', 'title': f'Library {i}',
                 'snippet': 'Collection catalogue lead.'} for i in range(first, first + count)]

    def provider(self, request):
        """Respond through the real SDK and inspect the actual executing-node payloads.

        Args:
            self: Fixture controlling deliberate failures and retaining task order.
            request: Actual SDK HTTP request; no alternative graph or parser is substituted.

        Returns:
            Synthetic provider HTTP response containing schema-shaped or deliberately bad JSON.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        self.tasks.append(title)
        if title == 'ResearchPlan':
            return self.cli.response()
        payload = json.loads(body['messages'][0]['content'])
        self.payloads.append((title, payload))
        if title == 'SearchSourceSelection':
            answer = {'selections': [{'result_id': r['result_id'], 'goal_ids': ['G1'], 'reason': 'Inspect catalogue.'}
                for r in payload['results']]}
        elif title == 'FollowupPlan':
            answer = {'gaps': [f'Unresolved collection detail {i}' for i in range(9)],
                'selections': [{'link_id': r['link_id'], 'goal_ids': ['G1'], 'reason': 'Inspect linked collection.'}
                    for r in payload['links'][:payload['max_selections']]], 'stop_reason': 'Useful collection leads.'}
            if self.fail_link:
                answer['selections'][0]['link_id'] = 'Invented'
        elif title == 'CoverageAssessment':
            answer = {'assessments': [{'criterion_id': 'K1', 'judgment': 'partial', 'items': [],
                'target_count': None, 'benchmark_candidate_ids': [], 'evidence_candidate_ids': [],
                'gaps': [f'Missing independent source {i}' for i in range(9)], 'rationale': 'Fixture only.'}],
                'next_searches': ([{'query': f'new collection {i}', 'goal_ids': ['G1'], 'reason': 'Follow gap.'}
                    for i in range(4)] if self.extra_coverage_queries and self.tasks.count('CoverageAssessment') == 1 else [])}
            if self.bad_coverage:
                answer['assessments'][0]['evidence_candidate_ids'] = ['NOT_OFFERED']
        elif title == 'KnowledgeBatch':
            answer = {'entities': [], 'assertions': [], 'remaining': False}
            if self.fail_capture and self.tasks.count('KnowledgeBatch') == 1:
                answer['remaining'] = 'not a boolean'
        elif title == 'ExtractionBatch':
            answer = {'entities': [], 'claims': []}
        else:
            raise AssertionError(title)
        return self.cli.response(text=json.dumps(answer))

    def run_pipeline(self, *flags, knowledge=False):
        """Invoke the public terminal interface with only provider transports replaced.

        Args:
            self: Fixture holding independent provider/source transports.
            *flags: Additional real terminal entry point options, including explicit limits.
            knowledge: Enable the normal separate graph-capture stage when True.

        Returns:
            terminal interface exit code, saved session and stderr. All files remain temporary.
        """
        with patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search), patch(
                'projectclarity.retrieval.public_url_fetcher.urllib3.HTTPSConnectionPool', side_effect=self.http.pool):
            code, _, err = self.cli.run_cli(self.provider, '--search', '--extract', '--review-claims', '--max-requests', '100',
                '--question', 'Identify community seed libraries and their collections.', *flags,
                knowledge=knowledge, include_urls=False, plan_only=False, details=False, source_dns=public_dns)
        _, session = self.cli.saved_session()
        return code, session, err

    def assert_proposals_accounted_for(self, session):
        """Check that every accepted query is attempted or still retained at a workflow boundary.

        Args:
            self: Test instance providing assertions over real saved production artifacts.
            session: Saved session whose planner and accepted coverage proposals define work.

        Returns:
            None. Fail if a proposal disappears, even when the terminal interface exits successfully or
            other queries run. This checks work conservation, not query relevance.
        """
        planned = {' '.join(q.split()).casefold() for step in session['plan']['strategy']
            for q in step['proposed_searches']}
        proposed = set(planned)
        for entry in session['coverage'].get('history', []):
            if entry.get('search_proposals_accepted'):
                proposed.update(' '.join(q['query'].split()).casefold()
                    for q in entry['proposal']['next_searches'])
        attempted = {' '.join(q['query'].split()).casefold() for q in session['search']['queries']}
        retained = planned | {' '.join(q['query'].split()).casefold()
            for q in session['coverage'].get('next_searches', [])}
        self.assertEqual(proposed - attempted, retained - attempted)

    def test_initial_queries_stay_first_then_directions_continue_beyond_four(self):
        """Protect the first planned pair, then reach all six queries despite one failure.

        Args:
            self: Independent two-direction search fixture.

        Returns:
            None. Assert real attempted order, retained failure and successful later retrieval.
        """
        self.fail_search = True
        code, session, _ = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assertEqual(self.queries, ['directory one', 'directory two', 'catalogue one',
            'catalogue two', 'directory three', 'catalogue three'])
        self.assertEqual(len(session['documents']), 5)
        self.assertIsNone(session['request']['max_search_queries'])
        self.assert_proposals_accounted_for(session)

    def test_explicit_query_cap_still_binds(self):
        """Keep the operator's cumulative attempt cap even when a search fails.

        Args:
            self: Fixture with six proposals and an explicit two-query cap.

        Returns:
            None. Assert exactly two attempts and no inferred completion.
        """
        self.fail_search = True
        _, session, _ = self.run_pipeline('--max-search-queries', '2')
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(session['search']['stop_reason'], 'search_query_limit')
        self.assert_proposals_accounted_for(session)

    def test_search_and_links_interleave_and_nine_gaps_do_not_block(self):
        """Give both discovery channels turns and accept nine descriptive gaps without truncation.

        Args:
            self: Fixture with independent search results and one link per retrieved result.

        Returns:
            None. Assert production task ordering and successful linked-page acquisition.
        """
        code, session, err = self.run_pipeline('--follow-links', '--max-followup-sources', '3')
        self.assertEqual(code, 0, err)
        actions = [t for t in self.tasks if t in {'SearchSourceSelection', 'FollowupPlan'}]
        self.assertEqual(actions[0], 'SearchSourceSelection')
        self.assertIn('FollowupPlan', actions[:3])
        self.assertIn('SearchSourceSelection', actions[3:])
        self.assertEqual(len(session['discovery']['selected_sources']), 6)
        self.assertTrue(all(len(h['proposal']['gaps']) == 9 for h in session['discovery']['history']))

    def test_capture_yields_resumes_old_windows_and_isolates_failure(self):
        """Keep discovery moving and drain old pending windows after a local capture failure.

        Args:
            self: Dense-page fixture with one malformed capture response and a one-call quantum.

        Returns:
            None. Assert failure isolation, later-source capture, final queue drain and honest status.
        """
        self.fail_capture = True
        code, session, _ = self.run_pipeline('--follow-links', '--max-search-queries', '2',
            '--knowledge-calls-per-round', '1', knowledge=True)
        self.assertEqual(code, 1)
        self.assertIn('search_query_limit', session['workflow']['stop_details'])
        first_extract = self.tasks.index('ExtractionBatch')
        # Search-selected sources now receive their primary first read before
        # optional broad capture; the later failure-isolation/drain checks remain.
        self.assertEqual(self.tasks[:first_extract].count('KnowledgeBatch'), 0)
        self.assertGreater(session['visited_nodes'].count('accumulate_knowledge'), 1)
        windows = session['knowledge']['windows']
        self.assertEqual(sum(w['status'] == 'failed' for w in windows), 1)
        self.assertFalse(any(w['status'] == 'pending' for w in windows))
        self.assertTrue(any(w['source_id'] == 'S1' and w['status'] == 'captured' for w in windows))
        self.assertTrue(any(w['source_id'] != 'S1' and w['status'] == 'captured' for w in windows))
        self.assertEqual(session['knowledge']['objective_status'], 'incomplete')
        self.assertTrue(any(c.get('error_code') == 'invalid_output_schema' and c['task'] == 'accumulate_knowledge'
            for c in session['processing_calls']))

    def test_four_coverage_queries_are_retained_and_executed(self):
        """A fourth proposed query is scheduling input, not an invalid assessment.

        Args:
            self: Fixture returning four searches once, then no new proposals.

        Returns:
            None. Assert all four are eventually attempted under shared budgets.
        """
        self.extra_coverage_queries = True
        code, session, err = self.run_pipeline()
        self.assertEqual(code, 0, err)
        self.assertTrue({f'new collection {i}' for i in range(4)}.issubset(self.queries))
        self.assertFalse(any(c.get('error_code') for c in session['processing_calls']))
        self.assert_proposals_accounted_for(session)

    def test_invalid_assessment_does_not_discard_valid_search_leads(self):
        """Continue independent research without accepting an unsupported coverage claim.

        Args:
            self: Fixture returning an unknown evidence ID alongside legitimate search goals.

        Returns:
            None. Assert new queries execute but all unsupported assessments remain rejected.
        """
        self.extra_coverage_queries = self.bad_coverage = True
        code, session, _ = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assertTrue({f'new collection {i}' for i in range(4)}.issubset(self.queries))
        self.assertTrue(all(h['error_code'] == 'unknown_or_changed_evidence' for h in session['coverage']['history']))
        self.assertTrue(session['coverage']['history'][0]['search_proposals_accepted'])
        self.assert_proposals_accounted_for(session)

    def test_budget_stop_retains_unexecuted_proposals_in_saved_session(self):
        """A legitimate budget stop must preserve the queue rather than making work disappear.

        Args:
            self: Fixture proposing four searches once with only one additional query allowed.

        Returns:
            None. Assert three coverage leads remain durably queued, all proposals are
            accounted for, and unresolved coverage is not converted into satisfaction.
        """
        self.extra_coverage_queries = True
        code, session, err = self.run_pipeline('--max-search-queries', '3')
        self.assertEqual(code, 1, err)
        self.assertEqual(session['workflow']['stop_reason'], 'budget_exhausted')
        self.assertEqual(session['search']['stop_reason'], 'search_query_limit')
        self.assertEqual({q['query'] for q in session['coverage']['next_searches']},
            {f'new collection {i}' for i in range(1, 4)})
        self.assertTrue(all(c['status'] == 'partial' for c in session['coverage']['criteria_progress']))
        self.assert_proposals_accounted_for(session)

    def test_surplus_ranked_results_are_deferred_not_rejected_or_lost(self):
        """Keep a twelve-result valid ranking across the ten-source processing batch.

        Args:
            self: Fixture whose first two queries each return six distinct result URLs.

        Returns:
            None. Assert all twelve are retrieved without another search or cap bypass.
        """
        self.many_results = True
        code, session, err = self.run_pipeline('--max-search-queries', '2', '--search-results-per-query', '6')
        self.assertEqual(code, 1, err)
        self.assertEqual(session['workflow']['stop_reason'], 'budget_exhausted')
        self.assertEqual(len(session['documents']), 12)
        self.assertEqual(len(session['search']['selections'][0]['deferred_ids']), 2)
        self.assertEqual(len(session['search']['selections']), 2)

    def test_fifteen_claims_reach_normal_evidence_validation(self):
        """Retain a complete fifteen-claim response without changing quote/reference validation.

        Args:
            self: Test owner creating an isolated extraction terminal interface fixture.

        Returns:
            None. Assert every claim becomes an anchored candidate, not an unvalidated record.
        """
        fixture = extraction_tests.KnowledgeExtractionIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        batch = proposed_batch()
        batch['claims'] = [{**copy.deepcopy(batch['claims'][0]), 'claim_id': f'C{i}'} for i in range(15)]
        code, _, err, _, session = fixture.run_extraction(batch=batch)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(session['extraction']['candidates']), 15)
        self.assertTrue(all(c['evidence_status'] == 'anchored' for c in session['extraction']['candidates']))

    def test_unicode_capture_labels_resolve_to_code_owned_graph_ids(self):
        """Accept local Unicode references while retaining actual quote and endpoint checks.

        Args:
            self: Test owner using an independent roster through the real capture terminal interface.

        Returns:
            None. Assert long Unicode labels retain all anchored relationships in storage.
        """
        fixture = knowledge_tests.KnowledgePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        mapping = {entity['entity_id']: 'référence locale détaillée ' + entity['entity_id'] * 12
            for entity in fixture.answer['entities']}
        for entity in fixture.answer['entities']:
            entity['entity_id'] = mapping[entity['entity_id']]
        for assertion in fixture.answer['assertions']:
            assertion['subject_id'] = mapping[assertion['subject_id']]
            if assertion['object_id'] is not None:
                assertion['object_id'] = mapping[assertion['object_id']]
        _, session, _, _ = fixture.run_capture()
        self.assertEqual(fixture.snapshots[0]['counts']['assertions'], 5)
        self.assertFalse(session['knowledge']['incomplete'])

    def test_long_coverage_label_keeps_its_final_qualification(self):
        """A long grouping description survives production coverage validation without truncation.

        Args:
            self: Test owner with an independent source-backed coverage fixture.

        Returns:
            None. Assert the full label is retained and the assessment succeeds.
        """
        fixture = search_tests.SearchCoveragePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        label = 'A detailed grouping description. ' * 12 + 'Affiliation remains uncertain.'

        def coverage(body):
            """Supply a long label with a valid offered evidence reference.

            Args:
                body: Actual serialized coverage request inspected by the shared responder.

            Returns:
                Synthetic provider HTTP containing the complete qualified grouping label.
            """
            answer = {'assessments': [{'criterion_id': 'K1', 'judgment': 'partial',
                'items': [{'key': label, 'candidate_ids': ['S1_P1_C1']}], 'target_count': None,
                'benchmark_candidate_ids': [], 'evidence_candidate_ids': ['S1_P1_C1'],
                'gaps': ['Verify current affiliation.'], 'rationale': 'Fixture only.'}], 'next_searches': []}
            return fixture.cli.response(text=json.dumps(answer))

        fixture.overrides['CoverageAssessment'] = coverage
        code, _, err, directory, session = fixture.run_pipeline('--max-search-queries', '1')
        self.assertEqual(code, 0, err)
        trace = json.loads((directory / 'llm_calls/coverage_1.json').read_text())
        self.assertEqual(trace['parsed_result']['assessments'][0]['items'][0]['key'], label)
        self.assertEqual(session['coverage']['assessment_status'], 'assessed')

    def test_search_warning_does_not_mask_invalid_link_reference(self):
        """Keep real semantic selector failures visible alongside earlier search errors.

        Args:
            self: Fixture with one search timeout and a deliberately invented link ID.

        Returns:
            None. Assert no invalid link is registered and both failures remain reported.
        """
        self.fail_search = self.fail_link = True
        code, session, _ = self.run_pipeline('--follow-links', '--max-search-queries', '2',
            '--knowledge-calls-per-round', '1', knowledge=True)
        self.assertEqual(code, 1)
        self.assertIn('invalid_selection_references', session['error'])
        self.assertIn('Search or source selection was incomplete', session['error'])
        self.assertFalse(session['discovery']['selected_sources'])

    def test_query_only_work_obeys_shared_clock_before_any_page_fetch(self):
        """Removing a query-count partition must not create an unbounded pre-retrieval loop.

        Args:
            self: Fixture whose first empty search advances the synthetic acquisition clock.

        Returns:
            None. Assert the second query never executes and the time stop remains visible.
        """
        elapsed = [1000.0]

        def empty_search(query, max_results, monitor=None):
            """Advance time inside one completed empty provider attempt.

            Args:
                query: Production-selected query retained to count actual attempts.
                max_results: Provider result allowance, unused for this empty response.
                monitor: Optional production progress monitor, not replaced by the fixture.

            Returns:
                An empty result list after advancing beyond the saved 180-second allowance.
            """
            self.queries.append(query)
            elapsed[0] += 181
            return []

        self.search = empty_search
        with patch('projectclarity.nodes.web_search.node.time.monotonic', side_effect=lambda: elapsed[0]):
            code, session, _ = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(session['search']['stop_reason'], 'fetch_budget_exhausted')
        self.assertFalse(self.http.requests)
