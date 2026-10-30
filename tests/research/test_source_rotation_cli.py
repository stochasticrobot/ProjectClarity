"""Check document switching through the public research command and workflow.

A long synthetic source and a preferred-domain alternative expose when repeated
low-yield reading should give another source or search a turn. Controlled provider
and search replies retain actual scheduling and saved outcomes. The assertions
measure allocation and restart behavior, not live source usefulness.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings
import json
import unittest
from unittest.mock import patch

from tests.research import test_document_continuation_cli as continuation
from tests.research import test_workflow_control_cli as control


class SourceRotationCLITests(unittest.TestCase):
    """Synthetic transports prove production ordering, not live extraction accuracy."""

    def setUp(self):
        """Prepare an isolated long-document harness and official alternative.

        Args:
            self: Case owning temporary run files and observed provider calls.
        Returns:
            None; scheduler, parsing, budget accounting and persistence remain real.
        """
        self.long = continuation.DocumentContinuationCLITests()
        self.long.setUp()
        self.addCleanup(self.long.doCleanups)
        self.flow = self.long.flow
        self.flow.cli.urls.write_text('https://example.org/research\n')
        self.flow.cli.plan['goals'][0]['result_shape'] = {
            'unit': 'researchers', 'required_fields': ['name', 'topic']}
        self.base_provider = self.flow.provider
        self.flow.provider = self.provider
        self.flow.search = self.search
        self.snapshots, self.payloads = [], []
        self.crash_official = False
        self.flow.routes[('ourcommons.ca', '/robots.txt')] = (
            200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n')
        self.flow.routes[('ourcommons.ca', '/roster')] = (
            200, {'Content-Type': 'text/plain'}, f'Name: {self.long.last_name} | Topic: AI security'.encode())

    def provider(self, request):
        """Add valid shaped item records to the transport's early and late findings.

        Args:
            self: Fixture recording real selection payloads and optional interruption.
            request: Real SDK request, including selected source and passage text.
        Returns:
            Provider-shaped synthetic reply, or a one-time abrupt worker exit.
            All output parsing, evidence validation and reviews execute normally.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        payload = json.loads(body['messages'][0]['content']) if title != 'ResearchPlan' else {}
        self.payloads.append((title, payload))
        if title == 'ExtractionBatch' and payload['source_id'] != 'S1' and self.crash_official:
            self.crash_official = False
            raise SystemExit(77)
        response = self.base_provider(request)
        if title == 'ExtractionBatch':
            answer = json.loads(response.json()['content'][0]['text'])
            names = {e['entity_id']: e['name'] for e in answer['entities']}
            for c in answer['claims']:
                relation = c['relationship']
                c['claim_kind'] = 'relational'
                c['result_records'] = [{'goal_id': 'G1', 'fields': [
                    {'name': 'name', 'value': names[relation['subject_id']]},
                    {'name': 'topic', 'value': names[relation['object_id']]}]}]
            return self.flow.cli.response(text=json.dumps(answer))
        return response

    def search(self, query, max_results, monitor=None, **kwargs):
        """Save a search-time snapshot and offer an official alternative.

        Args:
            self: Case retaining immutable saved snapshots.
            query: Query chosen by the production planner or coverage node.
            max_results: Requested result ceiling, respected by this single result.
            monitor: Unmodified production observer.
            **kwargs: Search-engine options; no actual engine is contacted.
        Returns:
            A synthetic result whose contents must still be fetched and extracted.
        """
        self.snapshots.append(self.flow.cli.saved_session()[1])
        return [{'url': 'https://ourcommons.ca/roster', 'title': 'Official roster', 'snippet': 'Member list'}][:max_results]

    def run_case(self, *flags):
        """Execute the production command with a small deterministic exploration wave.

        Args:
            self: Long-document fixture.
            *flags: Real command-line overrides, including concurrency and budget.
        Returns:
            Actual exit code, run directory, saved session and terminal output.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            return self.flow.run_workflow('--search', '--max-search-queries', '2',
                '--explore-before-search-depth', '1', '--max-requests', '70', *flags)

    def test_plateau_searches_early_reads_official_then_preserves_late_original_target(self):
        """Prove a real low-yield switch and later return under concurrent extraction.

        Args:
            self: Long roster with an empty middle and an official alternative.
        Returns:
            None; inspect persisted candidates, pause receipts and actual call order.
        """
        _, _, saved, progress = self.run_case('--model-concurrency', '3')
        self.assertTrue(self.snapshots)
        first = self.snapshots[0]
        self.assertEqual(first['workflow']['source_rotation']['status'], 'active')
        self.assertGreaterEqual(first['workflow']['source_rotation']['no_gain_passages'], 3)
        self.assertTrue(any(a['source_id'] == 'S1' and a['status'] == 'not_attempted'
            for a in first['extraction']['attempts']))
        selected = next(s['source_id'] for s in saved['sources'] if s['url'] == 'https://ourcommons.ca/roster')
        selection_index = next(i for i, (title, _) in enumerate(self.payloads) if title == 'SearchSourceSelection')
        after = self.payloads[selection_index + 1:]
        next_extraction = next(payload for title, payload in after if title == 'ExtractionBatch')
        self.assertEqual(next_extraction['source_id'], selected)
        self.assertTrue(any(c['source_id'] == selected and c['adjudication_status'] == 'supported'
            for c in saved['extraction']['candidates']))
        self.assertTrue(any(c['source_id'] == 'S1' and self.long.last_name in c['proposal']['statement']
            for c in saved['extraction']['candidates']))
        self.assertIn('recent passages added no new goal-level findings', progress)
        summary = next(p['registered_sources'] for t, p in self.payloads if t == 'SearchSourceSelection')
        self.assertGreater(summary[0]['completed_passages'], 0)
        self.assertTrue(summary[0]['goals'])
        self.assertLessEqual(saved['usage']['requests_attempted'], 70)

    def test_resume_does_not_replay_uncertain_handoff_or_lose_paused_work(self):
        """Persist a paused document through an interrupted official first read.

        Args:
            self: Fixture whose first official extraction exits after reservation.
        Returns:
            None; one planner and one uncertain wire attempt remain, while original
            unread work continues after loading the real checkpoint.
        """
        self.crash_official = True
        code, _, before, _ = self.run_case('--model-concurrency', '1')
        self.assertEqual(code, 77)
        self.assertEqual(before['workflow']['source_rotation']['status'], 'active')
        with patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search), patch(
                'projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, saved = self.flow.resume(before['session_id'])
        calls = [(p['source_id'], p['passage']['passage_id']) for t, p in self.payloads if t == 'ExtractionBatch']
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(sum(t == 'ResearchPlan' for t, _ in self.payloads), 1)
        self.assertTrue(any(a.get('error_code') == 'interrupted_outcome_unknown'
            for a in saved['extraction']['attempts']))
        self.assertTrue(any(c['source_id'] == 'S1' and self.long.last_name in c['proposal']['statement']
            for c in saved['extraction']['candidates']))


class LateSelectionCLITests(unittest.TestCase):
    """Reproduce the last-four-calls selection trap with real scheduler budgets."""

    def install_claim_provider(self, flow):
        """Make newly selected pages yield a quoted claim requiring an actual review.

        Args:
            self: Case whose transport fixture stays isolated.
            flow: Production harness with its ordinary planner/search responder.
        Returns:
            None. Replace only the model transport, not extraction or review nodes.
        """
        base = flow.provider

        def provider(request):
            """Return one anchored new-source claim and approve its supplied quote.

            Args:
                request: Actual serialized provider request.
            Returns:
                Synthetic structured response; production validators and the real
                shared request counter still decide whether this work succeeds.
            Review replies include authored report-mapping judgments; these do not
            establish live semantic recognition accuracy.
            """
            body = json.loads(request.content)
            title = body['output_config']['format']['schema']['title']
            payload = json.loads(body['messages'][0]['content']) if title != 'ResearchPlan' else {}
            if title == 'ExtractionBatch' and payload['source_id'] != 'S1':
                passage = payload['passage']
                return flow.cli.response(text=json.dumps({'entities': [], 'claims': [{
                    'claim_id': 'C1', 'claim_kind': 'non_relational', 'statement': passage['text'],
                    'goal_ids': ['G1'], 'entity_ids': [], 'qualifications': [],
                    'research_roles': ['answer_evidence'], 'relationship': None,
                    'evidence': [{'passage_id': passage['passage_id'], 'quote': passage['text']}]}]}))
            if title == 'ClaimReviewBatch':
                return flow.cli.response(text=json.dumps({'assessments': [
                    {'candidate_id': c['candidate_id'], 'verdict': 'supported', 'in_scope': True,
                     'rationale': 'Synthetic source support.',
                     'report_mappings': fixture_mappings(c, payload), 'evidence_quotes': [c['proposal']['evidence'][0]['quote']]}
                    for c in payload['candidates']]}))
            return base(request)

        flow.provider = provider

    def test_late_selection_retains_extraction_and_review_capacity(self):
        """Read an accepted official result instead of selecting more unprocessed pages.

        Args:
            self: Test owning an isolated seven-call run including final coverage.
        Returns:
            None; actual source extraction follows selection within the hard budget.
        """
        flow = control.WorkflowControlCLITests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        self.install_claim_provider(flow)
        flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/plain'}, b'Background only.')
        flow.search = lambda *args, **kwargs: [
            {'url': f'https://example.org/{path}', 'title': path, 'snippet': 'A lead'}
            for path in ('other', 'first', 'second')]
        _, _, saved, _ = flow.run_workflow('--search', '--max-requests', '7',
            '--max-search-queries', '3', '--explore-before-search-depth', '1')
        selections = [c for c in saved['processing_calls'] if c['task'] == 'select_sources']
        self.assertEqual(len(selections), 1)
        self.assertEqual(saved['search']['selections'][0]['capacity'], 1)
        chosen = saved['search']['selected_sources'][0]['source_id']
        self.assertTrue(any(a['source_id'] == chosen and a['status'] == 'completed'
            for a in saved['extraction']['attempts']))
        self.assertLessEqual(saved['usage']['requests_attempted'], 7)
        self.assertTrue(saved['search']['selections'][0]['deferred_ids'])
        self.assertTrue(any(c['source_id'] == chosen and c['adjudication_status'] == 'supported'
            for c in saved['extraction']['candidates']))

    def test_selection_retry_cannot_spend_reserved_first_read_calls(self):
        """Keep a read opportunity even when selection uses its configured retry.

        Args:
            self: Isolated production run whose first selector wire call times out.
        Returns:
            None; two selection attempts retain one accepted source and its real
            extraction while the global eight-call allowance remains binding.
        """
        import httpx2
        flow = control.WorkflowControlCLITests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        self.install_claim_provider(flow)
        flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/plain'}, b'Background only.')
        flow.search = lambda *args, **kwargs: [
            {'url': f'https://example.org/{path}', 'title': path, 'snippet': 'A lead'}
            for path in ('other', 'first', 'second')]
        base, timed_out = flow.provider, []

        def provider(request):
            """Inject one retryable transport failure, leaving production recovery intact.

            Args:
                request: Real serialized provider request.
            Returns:
                Synthetic normal response after the first selector timeout; initially
                raises a real transport timeout rather than manufacturing a receipt.
            """
            title = json.loads(request.content)['output_config']['format']['schema']['title']
            if title == 'SearchSourceSelection' and not timed_out:
                timed_out.append(True)
                raise httpx2.ReadTimeout('Synthetic selector timeout', request=request)
            return base(request)

        flow.provider = provider
        with patch('projectclarity.providers.model_retry.time.sleep'):
            _, _, saved, _ = flow.run_workflow('--search', '--max-requests', '8', '--model-retries', '1',
                '--max-search-queries', '3', '--explore-before-search-depth', '1')
        self.assertTrue(timed_out)
        chosen = saved['search']['selected_sources'][0]['source_id']
        self.assertTrue(any(a['source_id'] == chosen and a['status'] == 'completed'
            for a in saved['extraction']['attempts']))
        self.assertEqual(sum(a['task'] == 'select_sources' for a in saved['model_attempts']), 2)
        self.assertLessEqual(saved['usage']['requests_attempted'], 8)
        self.assertTrue(any(c['source_id'] == chosen and c['adjudication_status'] == 'supported'
            for c in saved['extraction']['candidates']))
