"""Check when research stops extending an unproductive chain of linked pages.

The command-line workflow reads synthetic pages and model replies while retaining
actual link selection, evidence processing and scheduler decisions. Cases cover
repeated claims, useful evidence upgrades, unfinished or failed analysis, alternate
paths and restart. A page reached through a blocked route must remain available
through a productive alternative. These checks do not measure live discovery quality.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings
import json
import re
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from projectclarity.discovery.branch_progress import branch_progress
from projectclarity.research_schemas import ResearchSession
from tests.research import test_workflow_control_cli as workflows


class BranchProgressCLITests(unittest.TestCase):
    """Require actual fetch decisions, evidence validation and saved counters."""

    def setUp(self):
        """Prepare the existing offline production-entry harness and evidence routing.

        Args:
            self: Test owner with temporary run files and prohibited live sockets.

        Returns:
            None. Replace only HTTP/DNS/model boundaries; no application node mocks.
        """
        self.flow = workflows.WorkflowControlCLITests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.base_provider = self.flow.provider
        self.flow.provider = self.provider
        self.pages = {}
        self.source_names = {}
        self.offers = []
        self.branch_snapshots = []
        self.crash_on = None

    def page(self, name, links=(), *, claim=None, quote=None, role='answer_evidence',
             verdict='supported', prefix='', malformed=False):
        """Add an HTML source and optional synthetic extraction/review outcome.

        Args:
            self: Fixture owning the route table and page-specific response data.
            name: Path component, with research naming the supplied root.
            links: Observed child path components; cycles and joins are permitted.
            claim: Optional assertion; None produces a valid empty extraction.
            quote: Optional exact source quotation; defaults to the assertion.
            role: Existing model-response research role, not a new instruction.
            verdict: Existing reviewer verdict or out_of_scope fixture shortcut.
            prefix: Extra text before the claim to exercise multi-passage analysis.
            malformed: True makes this page's extraction return invalid JSON.

        Returns:
            None. Install a successful source response and model fixture metadata.
        """
        quote = quote or claim
        self.pages[name] = dict(claim=claim, quote=quote, role=role, verdict=verdict, malformed=malformed)
        body = f'<main><pre>Page {name}.\n{prefix}{quote or "No relevant findings."}</pre>'
        body += ''.join(f'<a href="/{child}">Explore {child}</a>' for child in links) + '</main>'
        self.flow.routes[('example.org', '/' + name)] = (200, {'Content-Type': 'text/html'}, body.encode())

    def provider(self, request):
        """Supply page-specific replies through the actual provider SDK interface.

        Args:
            self: Fixture recording model calls and the real offered link inventory.
            request: HTTP request serialized by the production model adapter.

        Returns:
            Provider-shaped reply or one deliberate interrupted model call for restart.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        if title not in {'ExtractionBatch', 'ClaimReviewBatch', 'FollowupPlan'}:
            return self.base_provider(request)
        payload = json.loads(body['messages'][0]['content'])
        if title == 'FollowupPlan':
            self.offers.append([urlsplit(row['url']).path for row in payload['links']])
            _, saved = self.flow.cli.saved_session()
            self.branch_snapshots.append(self.decisions(saved))
            return self.base_provider(request)
        if title == 'ClaimReviewBatch' and 'passages' in payload:
            self.flow.tasks.append((title, None, None))
            assessments = []
            for candidate in payload['candidates']:
                page = self.pages[self.source_names[candidate['source_id']]]
                assessments.append({'candidate_id': candidate['candidate_id'],
                    'verdict': 'supported' if page['verdict'] == 'out_of_scope' else page['verdict'],
                    'in_scope': page['verdict'] != 'out_of_scope', 'rationale': 'Synthetic passage assessment.',
                    'report_mappings': fixture_mappings(candidate, payload), 'evidence_quotes': [page['quote']]})
            return self.flow.cli.response(text=json.dumps({'assessments': assessments}))
        sid = payload['source_id']
        match = re.search(r'Page (\w+)\.', payload['passage']['text'])
        if match:
            self.source_names[sid] = match.group(1)
        name = self.source_names[sid]
        page = self.pages[name]
        self.flow.tasks.append((title, sid, payload['passage']['passage_id']))
        if title == 'ExtractionBatch':
            if self.crash_on == name:
                self.crash_on = None
                raise SystemExit(77)
            if page['malformed']:
                return self.flow.cli.response(text='{')
            claims = []
            if page['claim'] and page['quote'] in payload['passage']['text']:
                claims = [{'claim_id': 'C1', 'statement': page['claim'], 'goal_ids': ['G1'],
                    'entity_ids': [], 'relationship': None, 'qualifications': [], 'research_roles': [page['role']],
                    'evidence': [{'passage_id': payload['passage']['passage_id'], 'quote': page['quote']}]}]
            answer = {'entities': [], 'claims': claims}
        else:
            answer = {'assessments': [{'candidate_id': c['candidate_id'],
                'verdict': 'supported' if page['verdict'] == 'out_of_scope' else page['verdict'],
                'in_scope': page['verdict'] != 'out_of_scope', 'rationale': 'Synthetic passage assessment.',
                'report_mappings': fixture_mappings(c, payload), 'evidence_quotes': [page['quote']]} for c in payload['candidates']]}
        return self.flow.cli.response(text=json.dumps(answer))

    def run_case(self, *flags):
        """Run normal terminal interface traversal with bounded synthetic calls and no pacing delay.

        Args:
            self: Fixture whose HTML determines the available crawl graph.
            *flags: Actual operator flags; default branch limit is not overridden.

        Returns:
            terminal interface result, persisted session and progress text from the existing harness.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            return self.flow.run_workflow('--max-requests', '100', *flags)

    def decisions(self, saved):
        """Expose saved per-page decisions by path instead of generated source IDs.

        Args:
            self: Test fixture; no mutation or I/O is performed.
            saved: Production session JSON containing the final branch ledger.

        Returns:
            Path-to-decision mapping from persisted, not fabricated, counters.
        """
        ledger = saved['workflow']['branch_progress']['sources']
        return {urlsplit(s['url']).path: ledger[s['source_id']] for s in saved['sources']}

    def test_default_three_empty_pages_stop_before_fourth(self):
        """Prove the new default through actual acquired pages, not just eligibility.

        Args:
            self: Empty chain with an unread fourth descendant.

        Returns:
            None. Assert depth-three cutoff, explicit stop reason and reload parity.
        """
        for name, child in [('research', 'a'), ('a', 'b'), ('b', 'c'), ('c', 'd'), ('d', 'end')]:
            self.page(name, [child])
        code, directory, saved, err = self.run_case()
        self.assertEqual(code, 0, err[-1500:])
        decisions = self.decisions(saved)
        self.assertEqual(set(decisions), {'/research', '/a', '/b', '/c'})
        self.assertEqual([decisions[p]['unproductive_count'] for p in ['/research', '/a', '/b', '/c']], [0, 1, 2, 3])
        self.assertEqual(saved['request']['max_unproductive_depth'], 3)
        self.assertEqual(saved['workflow']['stop_reason'], 'branch_limit_reached')
        self.assertIn('consecutive unproductive pages', err)
        events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
        stops = [e for e in events if e['event'] == 'branch_progress_changed'
            and e['details']['branch_status'] == 'branch_unproductive_limit']
        self.assertTrue(stops)
        self.assertEqual(stops[-1]['details']['unproductive_count'], 3)
        self.assertFalse(any('/d' in offers for offers in self.offers))
        loaded = ResearchSession.model_validate_json((directory / 'session.json').read_text())
        self.assertEqual(branch_progress(loaded), saved['workflow']['branch_progress'])

    def test_limit_two_backtracks_to_sibling_and_useful_claim_resets(self):
        """Keep a productive sibling after its neighbor exhausts a shared parent path.

        Args:
            self: Branching graph with a one-link selector batch to expose backtracking.

        Returns:
            None. Assert C cannot reach D, while E resets and reaches F then G.
        """
        self.page('research', ['a'])
        self.page('a', ['b'], claim='Group A researches attacks.')
        self.page('b', ['c', 'e'])
        self.page('c', ['d'])
        self.page('e', ['f'], claim='Group E researches defenses.')
        self.page('f', ['g'])
        self.page('g', ['h'])
        code, _, saved, err = self.run_case('--max-unproductive-depth', '2', '--max-followup-sources', '1')
        self.assertEqual(code, 0, err[-1500:])
        decisions = self.decisions(saved)
        self.assertNotIn('/d', decisions)
        self.assertNotIn('/h', decisions)
        self.assertEqual(decisions['/e']['unproductive_count'], 0)
        self.assertEqual(decisions['/g']['unproductive_count'], 2)
        self.assertTrue(all(d['status'] == 'retrieved' for d in saved['documents']))

    def test_repeated_claims_do_not_keep_a_branch_alive(self):
        """New source IDs and repeated reviews are not new research information.

        Args:
            self: Repeated exact statement and quotation across four pages.

        Returns:
            None. Assert copies increment dry counters even with supported verdicts.
        """
        for name, child in [('research', 'a'), ('a', 'b'), ('b', 'c')]:
            self.page(name, [child], claim='Group A researches attacks.')
        _, _, saved, _ = self.run_case('--max-unproductive-depth', '2')
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/b']['unproductive_count'], 2)
        self.assertEqual(decisions['/b']['signals'], [])
        self.assertNotIn('/c', decisions)

    def test_supported_upgrade_of_grounded_lead_resets(self):
        """A later reviewed support upgrade can be progress without a new statement.

        Args:
            self: Root lead with insufficient support, then supported same assertion.

        Returns:
            None. Assert support_upgrade at A and exploration reaches depth three.
        """
        self.page('research', ['a'], claim='Group A researches attacks.', role='lead', verdict='insufficient_evidence')
        self.page('a', ['b'], claim='Group A researches attacks.')
        self.page('b', ['c'])
        self.page('c', ['d'])
        _, _, saved, _ = self.run_case('--max-unproductive-depth', '2')
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/a']['signals'][0]['reason'], 'support_upgrade')
        self.assertEqual(decisions['/c']['unproductive_count'], 2)
        self.assertNotIn('/d', decisions)

    def test_new_quote_supported_evidence_and_promising_lead_count(self):
        """Existing roles and anchored evidence cover both useful continuation cases.

        Args:
            self: Same claim with a different supporting quote, then a grounded new lead.

        Returns:
            None. Assert evidence and lead progress resets without a confidence field.
        """
        self.page('research', ['a'], claim='Group A studies attacks.')
        self.page('a', ['b'], claim='Group A studies attacks.', quote='Group A publishes attack experiments.')
        self.page('b', ['c'], claim='A project page names Group B.', role='lead', verdict='insufficient_evidence')
        self.page('c', ['d'])
        self.page('d', ['end'])
        _, _, saved, _ = self.run_case('--max-unproductive-depth', '2')
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/a']['signals'][0]['reason'], 'additional_supporting_evidence')
        self.assertEqual(decisions['/b']['signals'][0]['reason'], 'new_grounded_lead')
        self.assertEqual(decisions['/d']['unproductive_count'], 2)

    def test_pending_late_passage_is_not_zero_yield(self):
        """Hold a child until late useful evidence arrives, instead of premature pruning.

        Args:
            self: Long non-seed page whose only useful claim is beyond passage one.

        Returns:
            None. Assert the late finding resets N=1 and allows its child to be fetched.
        """
        self.page('research', ['a'])
        self.page('a', ['b'], claim='Late evidence names Group A.', prefix='Background context only.\n' * 1100)
        self.page('b', ['c'])
        _, _, saved, _ = self.run_case('--max-unproductive-depth', '1')
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/a']['status'], 'useful')
        self.assertIn('/b', decisions)
        self.assertNotIn('/c', decisions)
        sid = next(s['source_id'] for s in saved['sources'] if s['url'].endswith('/a'))
        self.assertGreater(sum(a['source_id'] == sid for a in saved['extraction']['attempts']), 1)
        a_calls = [i for i, t in enumerate(self.flow.tasks) if t[0] == 'ExtractionBatch' and t[1] == sid]
        b_call = next(i for i, t in enumerate(self.flow.tasks) if t[0] == 'ExtractionBatch' and self.source_names.get(t[1]) == 'b')
        self.assertGreater(b_call, max(a_calls))

    def test_failed_analysis_is_unknown_and_sibling_still_runs(self):
        """A malformed extraction pauses its branch without inventing an empty result.

        Args:
            self: One failed child and an independent useful sibling.

        Returns:
            None. Assert unknown/no counter for failure, useful sibling acquisition.
        """
        self.page('research', ['a', 'other'])
        self.page('a', ['b'], malformed=True)
        self.page('other', ['next'], claim='Other group researches privacy.')
        self.page('next')
        code, _, saved, _ = self.run_case('--max-unproductive-depth', '1')
        self.assertEqual(code, 1)
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/a']['status'], 'unknown')
        self.assertIsNone(decisions['/a']['unproductive_count'])
        self.assertEqual(decisions['/a']['expansion_block'], 'branch_analysis_pending')
        self.assertIn('/next', decisions)
        self.assertNotIn('/b', decisions)

    def test_restart_preserves_pruned_branch_and_does_not_repeat_model_call(self):
        """Resume the public journal while keeping one exhausted branch closed.

        Args:
            self: An interrupted sibling extraction after a first branch was exhausted.

        Returns:
            None. Assert saved threshold/counter, no forbidden fetch and no replanning.
        """
        self.page('research', ['a', 'other'])
        self.page('a', ['b'])
        self.page('other', ['next'])
        self.crash_on = 'other'
        code, _, saved, _ = self.run_case('--max-unproductive-depth', '1', '--max-followup-sources', '1')
        self.assertEqual(code, 77)
        before = len(self.flow.tasks)
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, resumed = self.flow.resume(saved['session_id'])
        decisions = self.decisions(resumed)
        self.assertEqual(decisions['/a']['unproductive_count'], 1)
        self.assertNotIn('/b', decisions)
        self.assertNotIn('/next', decisions)
        self.assertFalse(any(t[0] == 'ResearchPlan' for t in self.flow.tasks[before:]))
        self.assertEqual(sum(t[0] == 'ExtractionBatch' and self.source_names.get(t[1]) == 'other' for t in self.flow.tasks), 1)

    def test_shared_page_reopens_through_productive_alternate_path_without_refetch(self):
        """A dry route cannot permanently close a shared page reached by useful work.

        Args:
            self: Diamond graph with a cycle and a later productive route into the join.

        Returns:
            None. Assert join first reaches its limit, later reopens through Q, and
            its child is fetched once; the shared page itself is never fetched twice.
        """
        self.page('research', ['a'])
        self.page('a', ['join', 'p'])
        self.page('join', ['x', 'a'])
        self.page('p', ['q'], claim='Group P studies privacy.')
        self.page('q', ['join'], claim='Group Q studies attacks.')
        self.page('x', ['end'])
        code, _, saved, err = self.run_case('--max-unproductive-depth', '2', '--max-followup-sources', '1')
        self.assertEqual(code, 0, err[-1500:])
        decisions = self.decisions(saved)
        self.assertTrue(any(s.get('/join', {}).get('unproductive_count') == 2 for s in self.branch_snapshots))
        self.assertEqual(decisions['/join']['unproductive_count'], 1)
        qid = next(s['source_id'] for s in saved['sources'] if s['url'].endswith('/q'))
        self.assertIn(qid, decisions['/join']['path'])
        self.assertEqual(sum(r[1] == '/join' for r in self.flow.http.requests), 1)
        self.assertEqual(sum(r[1] == '/x' for r in self.flow.http.requests), 1)
        self.assertNotIn('/end', decisions)

    def test_context_rejected_and_out_of_scope_claims_do_not_reset(self):
        """Require research usefulness, not merely a growing candidate count.

        Args:
            self: Three-page branch containing fresh but nonqualifying assertions.

        Returns:
            None. Assert all three count as no progress and the fourth is not offered.
        """
        self.page('research', ['a'])
        self.page('a', ['b'], claim='The building has a blue door.', role='context')
        self.page('b', ['c'], claim='Group B studies attacks.', verdict='contradicted')
        self.page('c', ['d'], claim='An unrelated group studies oceans.', verdict='out_of_scope')
        _, _, saved, _ = self.run_case()
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/c']['unproductive_count'], 3)
        self.assertTrue(all(not decisions[p]['signals'] for p in ['/a', '/b', '/c']))
        self.assertNotIn('/d', decisions)

    def test_alternate_history_survives_cycle_without_using_repeated_node(self):
        """Keep a valid alternate route when the first route would require a cycle.

        Args:
            self: Two routes into productive Y, one already containing shared X.

        Returns:
            None. Assert the other route reaches X with a lower counter and opens Z.
        """
        self.page('research', ['a', 'b'])
        self.page('a', ['x'])
        self.page('b', ['c'])
        self.page('c', ['y'])
        self.page('x', ['y', 'z'])
        self.page('y', ['x'], claim='Group Y publishes security research.')
        self.page('z', ['end'])
        self.page('end')
        _, _, saved, _ = self.run_case()
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/x']['unproductive_count'], 1)
        self.assertEqual(decisions['/z']['unproductive_count'], 2)
        self.assertIn('/end', decisions)
        self.assertEqual(len(decisions['/x']['path']), len(set(decisions['/x']['path'])))

    def test_path_state_bound_reports_unknown_instead_of_false_pruning(self):
        """The route-computation backstop must not masquerade as no research progress.

        Args:
            self: Simple graph with a deliberately lowered internal state bound.

        Returns:
            None. Assert explicit blocked status and no false branch-limit decision.
        """
        self.page('research', ['a'])
        self.page('a', ['b'])
        with patch('projectclarity.discovery.branch_progress.PATH_STATE_LIMIT', 1):
            code, _, saved, err = self.run_case('--max-unproductive-depth', '1')
        self.assertEqual(code, 1, err[-1500:])
        self.assertEqual(saved['workflow']['stop_reason'], 'blocked')
        self.assertIn('branch_path_search_limit', saved['workflow']['stop_details'])
        ledger = saved['workflow']['branch_progress']
        self.assertFalse(ledger['path_search_complete'])
        self.assertEqual(ledger['path_states_generated'], 1)
        decisions = self.decisions(saved)
        self.assertEqual(decisions['/a']['status'], 'empty')
        self.assertIsNone(decisions['/a']['unproductive_count'])
        self.assertEqual(decisions['/a']['expansion_block'], 'branch_path_search_limit')
        self.assertNotIn('/b', decisions)
        self.assertIn('unresolved paths are paused', err)
