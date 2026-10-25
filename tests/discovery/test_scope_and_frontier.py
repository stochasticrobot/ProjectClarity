"""Exercise early estimates and non-prefix discovery through normal CLI/graph paths.

Only model/search/source transports are fixtures. These tests prove data flow and
budget behavior, not live model relevance, population calibration or researcher recall.
No unfinished knowledge-capture windows are resumed by these changes.
"""
import copy
import json
import unittest

from projectclarity.coverage.coverage_contracts import CoverageAssessment
from projectclarity.coverage.scope_estimation import initialize_scope, revise_scope
from projectclarity.research_schemas import ScopeEstimate
from tests.discovery import test_search_coverage_pipeline as search_tests
from tests.claim_review import test_review_and_discovery_integration as review_tests
from tests.extraction.extraction_test_fixtures import SOURCE_TEXT, proposed_batch
from tests.retrieval.retrieval_test_fixtures import public_dns


class ScopeAndFrontierTests(unittest.TestCase):
    """Use invented unrelated leads to falsify source-order starvation and frozen scope."""

    def test_coverage_receives_complete_claim_and_all_supporting_quotes(self):
        """Keep a late qualification and third quotation visible in the real coverage call.

        Args:
            self: Test case using the normal CLI, parser and model request serialization.

        Returns:
            None. Fail if payload construction silently removes a decisive suffix or quote.
            This checks delivery, not whether a live model reasons correctly about them.
        """
        base = review_tests.ReviewDiscoveryIntegrationTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        statement = "Alice works at Lab X on " + "documentation and coordination, " * 15 + "but not as a current member."
        quotes = [statement, "The role ended in 2024.", "This is historical membership only."]
        base.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/plain'},
            ('\n'.join(quotes) + '\n').encode())
        calls = []

        def extract(body):
            """Supply a long qualification-bearing claim through the fake provider transport.

            Args:
                body: Real serialized extraction request, unused by this fixed response.

            Returns:
                Provider-shaped response whose complete quotations occur in the source.
            """
            answer = proposed_batch()
            answer['claims'][0].update(statement=statement,
                evidence=[{'passage_id': 'P1', 'quote': quote} for quote in quotes])
            return base.cli.response(text=json.dumps(answer))

        def assess(body):
            """Inspect actual coverage input before returning an intentionally partial result.

            Args:
                body: Real provider request containing the offered evidence inventory.

            Returns:
                Provider-shaped assessment that makes no completion claim.
            """
            payload = json.loads(body['messages'][0]['content'])
            calls.append(payload)
            self.assertEqual(payload['candidates'][0]['statement'], statement)
            self.assertEqual(payload['candidates'][0]['quotes'], quotes)
            self.assertFalse(payload['search_allowed'])
            self.assertIn('research_date', payload)
            self.assertIn('working_assumptions', payload)
            return base.cli.response(text=json.dumps({'assessments': [{
                'criterion_id': 'K1', 'judgment': 'partial', 'items': [], 'target_count': None,
                'benchmark_candidate_ids': [], 'evidence_candidate_ids': [],
                'gaps': ['Current membership remains unknown.'], 'rationale': 'Historical evidence only.'}],
                'next_searches': []}))

        base.overrides.update(ExtractionBatch=extract, CoverageAssessment=assess)
        code, _, err = base.cli.run_cli(base.provider, '--extract', '--assess-coverage', '--max-requests', '6',
            plan_only=False, direct_entry=True, source_dns=public_dns, details=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)

    def test_discovered_queries_execute_after_five_sources_and_two_rounds(self):
        """Follow evidence-driven searches past both historical hidden partitions.

        Args:
            self: Test owner of the actual CLI with fake search/model/source transports.

        Returns:
            None. Assert three search rounds and seven retrieved sources within the
            explicit query/global-source budgets; no real external calls occur.
        """
        base = search_tests.SearchCoveragePipelineTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        for i in range(1, 8):
            base.http.routes[('example.org', f'/lead{i}')] = (200, {'Content-Type': 'text/plain'}, SOURCE_TEXT.encode())
        queries = []
        def search(query, max_results, monitor=None):
            """Expose five initial results and one fresh lead in each subsequent round.

            Args:
                query: Actual query submitted by the production search node.
                max_results: Provider result allowance; each fixture batch respects it.
                monitor: Optional production observer; the caller owns query telemetry.

            Returns:
                Observed source leads with no fabricated supporting-evidence claims.
            """
            queries.append(query)
            numbers = range(1, 6) if len(queries) == 1 else [len(queries) + 4]
            return [{'url': f'https://example.org/lead{i}', 'title': f'Group lead {i}', 'snippet': 'A lead.'}
                for i in numbers]
        def select(body):
            """Select observed result IDs to test actual quota enforcement and routing.

            Args:
                body: Serialized production source-selection request.

            Returns:
                Provider response selecting the current fixture batch within capacity.
            """
            payload = json.loads(body['messages'][0]['content'])
            return base.cli.response(text=json.dumps({'selections': [
                {'result_id': r['result_id'], 'goal_ids': ['G1'], 'reason': 'Investigate the group lead.'}
                for r in payload['results'][:payload['capacity']]]}))
        def assess(body):
            """Generate successive new queries from the real current evidence payload.

            Args:
                body: Actual coverage request after the newly retrieved source batch.

            Returns:
                Provider-shaped assessment with one new query until three rounds finish.
            """
            payload = json.loads(body['messages'][0]['content'])
            cid = payload['candidates'][-1]['candidate_id']
            return base.cli.response(text=json.dumps({'assessments': [{'criterion_id': 'K1',
                'judgment': 'partial', 'items': [{'key': 'Alice', 'candidate_ids': [cid]}],
                'target_count': None, 'benchmark_candidate_ids': [], 'evidence_candidate_ids': [cid],
                'gaps': ['More independent groups may exist.'], 'rationale': 'New group lead warrants investigation.'}],
                'next_searches': [] if len(queries) == 3 else [{'query': f'new group partners round {len(queries) + 1}',
                    'goal_ids': ['G1'], 'reason': f'Follow evidence from {cid}.'}]}))
        def extract(body):
            """Return the same anchored fixture claim for each independently fetched source.

            Args:
                body: Production extraction request accepted for transport compatibility.

            Returns:
                One ordinary model-shaped claim; repeated sightings are not novel people.
            """
            return base.cli.response(text=json.dumps(proposed_batch()))
        base.overrides.update(SearchSourceSelection=select, CoverageAssessment=assess, ExtractionBatch=extract)
        code, _, err, _, session = base.run_pipeline('--question', 'Discover standards organizations and their partners.',
            search=search, budget=24)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(queries), 3)
        self.assertEqual(session['visited_nodes'].count('web_search'), 3)
        self.assertEqual(len(session['search']['selected_sources']), 7)
        self.assertEqual(sum(d['status'] == 'retrieved' for d in session['documents']), 7)
        self.assertIsNone(session['request']['max_search_sources'])

    def test_initial_scope_precedes_search_and_revisions_preserve_history(self):
        """Keep the early prior and two evidence-linked revisions on the production route.

        Args:
            self: Test owner of isolated transport fixtures and assertions.

        Returns:
            None. Assert the prior appears before search, revisions reach saved output,
            and none becomes a verified completion denominator by itself.
        """
        base = search_tests.SearchCoveragePipelineTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.cli.plan['scope_estimates'][0]['unit'] = 'distinct members'
        def assess(body):
            """Return two model-shaped revisions using only actual offered candidate IDs.

            Args:
                body: Real serialized SDK request delivered to the fixture transport.

            Returns:
                Provider reply estimating a narrower range on each new evidence round.
            """
            payload = json.loads(body['messages'][0]['content'])
            estimate = copy.deepcopy(payload['current_scope_estimates'][0])
            last = len(payload['candidates']) > 1
            estimate.update(lower=1 if last else 2, upper=4 if last else 20,
                basis='evidence_informed', candidate_ids=[payload['candidates'][-1]['candidate_id']],
                rationale='New fixture evidence revises the prior; this is not calibrated.')
            answer = {'assessments': [{'criterion_id': 'K1', 'judgment': 'partial',
                'items': [{'key': 'Alice', 'candidate_ids': ['S1_P1_C1']}], 'target_count': None,
                'benchmark_candidate_ids': [], 'evidence_candidate_ids': ['S1_P1_C1'],
                'gaps': ['Population estimate remains uncertain.'], 'rationale': 'Fixture evidence only.'}],
                'scope_estimates': [estimate], 'next_searches': [] if last else [
                    {'query': 'independent membership evidence', 'goal_ids': ['G1'], 'reason': 'Test the early scope estimate.'}]}
            return base.cli.response(text=json.dumps(answer))
        base.overrides['CoverageAssessment'] = assess
        code, out, err, directory, session = base.run_pipeline('--question', 'Identify members of an independent standards group.')
        self.assertEqual(code, 0, err)
        self.assertLess(err.index('Scope E1: 10–200'), err.index('Search the web'))
        history = session['coverage']['scope_estimate_history']
        self.assertEqual([h['status'] for h in history], ['initial_prior', 'updated', 'updated'])
        self.assertEqual([h['estimates'][0]['upper'] for h in history], [200, 20, 4])
        self.assertEqual(session['plan']['scope_estimates'][0]['upper'], 200)
        self.assertIn('Scope E1: 1–4 distinct members', out)
        self.assertIsNone(session['coverage']['criteria_progress'][0]['target_count'])
        self.assertEqual(json.loads((directory / 'coverage.json').read_text())['scope_estimate_history'], history)

    def test_invalid_scope_revision_retains_prior_and_bad_ranges_fail(self):
        """Reject invented evidence and inconsistent ranges instead of laundering a prior.

        Args:
            self: Test instance using schema/validation helpers without model transport.

        Returns:
            None. Assert invalid references preserve the prior and reversed ranges fail.
        """
        base = search_tests.SearchCoveragePipelineTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        from projectclarity.research_schemas import ResearchPlan
        from types import SimpleNamespace
        plan = ResearchPlan.model_validate(base.cli.plan)
        coverage = {}
        initialize_scope(coverage, plan)
        original = copy.deepcopy(coverage['scope_estimates'])
        bad = copy.deepcopy(original[0])
        bad.update(basis='evidence_informed', candidate_ids=['invented'], lower=1000, upper=2000)
        answer = CoverageAssessment(assessments=[], next_searches=[], scope_estimates=[ScopeEstimate(**bad)])
        revise_scope(coverage, answer, SimpleNamespace(plan=plan), {}, 1)
        self.assertEqual(coverage['scope_estimates'], original)
        self.assertEqual(coverage['scope_estimate_history'][-1]['status'], 'invalid_revision')
        bad.update(lower=20, upper=10)
        with self.assertRaises(ValueError):
            ScopeEstimate(**bad)

    def test_large_first_page_and_empty_context_batches_do_not_hide_other_leads(self):
        """Retain all anchors and reach a later source plus a deferred tail link through the CLI.

        Args:
            self: Test owner of source/model HTTP fixtures; the selector is not mocked.

        Returns:
            None. Assert both useful leads are fetched, oversized context is paged rather
            than dropped, and empty batches do not terminate pending frontier work.
            The explicit four-page cap leaves full-batch deferred offers unfinished.
        """
        base = review_tests.ReviewDiscoveryIntegrationTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.cli.urls.write_text('https://example.org/research\nhttps://example.org/second\n')
        tail = '/'+('z' * 500)+'/wanted'
        links = ''.join(f'<a href="/{"x" * 500}/{i}">Noise</a>' for i in range(300))
        links += f'<a href="{tail}">Useful late lead</a>'
        for path, anchors in [('/research', links), ('/second', '<a href="/other">Useful other source</a>'),
                              ('/other', ''), (tail, '')]:
            base.routes[('example.org', path)] = (200, {'Content-Type': 'text/html'},
                ('<main><p>' + SOURCE_TEXT + '</p>' + anchors + '</main>').encode())
        offers = []
        def choose(body):
            """Select useful fixture-labelled rows only after the real scheduler offers them.

            Args:
                body: Actual adapter request containing its context-budgeted frontier.

            Returns:
                Provider response with zero or more useful observed IDs, never invented URLs.
            """
            payload = json.loads(body['messages'][0]['content'])
            offers.append(payload)
            rows = [l for l in payload['links'] if l['label'].startswith('Useful')]
            return base.cli.response(text=json.dumps({'gaps': ['Investigate fresh directions.'],
                'selections': [{'link_id': l['link_id'], 'goal_ids': ['G1'], 'reason': 'Useful fixture lead.'}
                    for l in rows[:payload['max_selections']]], 'stop_reason': 'This context batch considered.'}))
        base.overrides['FollowupPlan'] = choose
        code, _, err = base.cli.run_cli(base.provider, '--question', 'Trace the members and partners of a standards group.',
            '--extract', '--review-claims', '--follow-links', '--max-requests', '40', '--max-sources', '4',
            plan_only=False, direct_entry=True, source_dns=public_dns, details=False)
        self.assertEqual(code, 1, err)
        _, session = base.cli.saved_session()
        self.assertIn('source_limit', session['workflow']['stop_details'])
        self.assertEqual(sum(d['status'] == 'retrieved' for d in session['documents']), 4)
        self.assertEqual(len(session['documents'][0]['links']), 301)
        self.assertFalse(session['documents'][0]['links_truncated'])
        self.assertIn('S2', {l['parent_source_id'] for offer in offers[:2] for l in offer['links']})
        self.assertTrue(any(p['context_inventory']['deferred'] for p in offers))
        self.assertGreater(len(offers), 2)
        self.assertIn('/other', [r[1] for r in base.http.requests])
        self.assertIn(tail, [r[1] for r in base.http.requests])
        self.assertIn('frontier_batch_deferred', [h['stop_reason'] for h in session['discovery']['history']])
        self.assertFalse(any(l['exclusion_reason'] == 'selection_input_cap' for l in session['discovery']['links']))

    def test_coverage_offers_fresh_claims_after_old_thirty_claim_boundary(self):
        """Pass all 48 intact claim records to real coverage and produce a new-source query.

        Repeated fixture statements test evidence visibility, not unique-person recall.

        Args:
            self: Test case using the production CLI and transport-only synthetic replies.

        Returns:
            None. Assert later-source evidence is offered and a query referencing it is
            retained; no source/claim-prefix cutoff may silently discard that evidence.
        """
        base = review_tests.ReviewDiscoveryIntegrationTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        paths = ['/research', '/two', '/three', '/four']
        base.cli.urls.write_text(''.join(f'https://example.org{path}\n' for path in paths))
        for path in paths:
            base.routes[('example.org', path)] = (200, {'Content-Type': 'text/plain'}, SOURCE_TEXT.encode())
        def extract(body):
            """Return twelve individually identified fixture claims per normal source call.

            Args:
                body: Serialized source extraction request accepted for transport compatibility.

            Returns:
                Provider reply with twelve anchored records; this is not twelve novel facts.
            """
            answer = proposed_batch()
            answer['claims'] = [{**copy.deepcopy(answer['claims'][0]), 'claim_id': f'C{i}'} for i in range(1, 13)]
            return base.cli.response(text=json.dumps(answer))
        def assess(body):
            """Verify later-source visibility before proposing a new targeted search.

            Args:
                body: Production coverage request, including all context-fit candidates.

            Returns:
                Provider response referencing evidence beyond the former thirty-claim prefix.
            """
            payload = json.loads(body['messages'][0]['content'])
            self.assertLessEqual(len(payload['candidates']), 48)
            self.assertFalse(payload['inventory_truncated'])
            offered_id = payload['candidates'][-1]['candidate_id']
            return base.cli.response(text=json.dumps({'assessments': [{'criterion_id': 'K1',
                'judgment': 'partial', 'items': [{'key': 'Alice', 'candidate_ids': [offered_id]}],
                'target_count': None, 'benchmark_candidate_ids': [], 'evidence_candidate_ids': [offered_id],
                'gaps': ['Follow new organization leads.'], 'rationale': 'Later-source evidence informs search.'}],
                'next_searches': [{'query': 'Lab X collaborators current membership', 'goal_ids': ['G1'],
                    'reason': 'Follow a later-source group lead.'}]}))
        base.overrides.update(ExtractionBatch=extract, CoverageAssessment=assess)
        code, _, err = base.cli.run_cli(base.provider, '--extract', '--assess-coverage', '--max-requests', '10',
            plan_only=False, direct_entry=True, source_dns=public_dns, details=False)
        self.assertEqual(code, 0, err)
        _, session = base.cli.saved_session()
        self.assertEqual(len(session['coverage']['history'][-1]['offered_candidate_ids']), 48)
        self.assertEqual(session['coverage']['next_searches'][0]['query'], 'Lab X collaborators current membership')
