"""Check continued reading of a useful long document without excluding other sources.

Synthetic tables, source replies and timing drive the real command-line workflow.
The tests place useful records near both ends and inspect passage attempts, periodic
other-source reads, coverage opportunities and saved restart state. Time and request
limits must retain unread work instead of dispatching it as repeated failures.
The scenarios verify scheduling, not a live model's extraction recall.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import json
import re
import time
import unittest
from unittest.mock import patch

from projectclarity.run_monitor import RunMonitor
from tests.research import test_workflow_control_cli as workflow_tests


class DocumentContinuationCLITests(unittest.TestCase):
    """Require whole productive sources, including late findings and refinements."""

    def setUp(self):
        """Install a long table, another seed and real-adapter synthetic responses.

        Args:
            self: Case owning isolated run files and transport controls.

        Returns:
            None. No actual network or credentials are used by the shared harness.
        """
        self.flow = workflow_tests.WorkflowControlCLITests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.base_provider = self.flow.provider
        self.flow.provider = self.provider
        self.flow.search = self.search
        self.flow.cli.urls.write_text('https://example.org/research\nhttps://example.org/other\n')
        self.flow.cli.plan['strategy'][0]['proposed_searches'] = ['broad research']
        self.flow.routes[('example.org', '/other')] = (200, {'Content-Type': 'text/plain'},
            b'Additional seed evidence.\n' * 1600)
        self.calls = []
        self.search_snapshots = []
        self.role = 'answer_evidence'
        self.crash_passage = None
        self.clock_offset = 0
        self.expire_after_passage = None
        self.install_table(160)

    def install_table(self, rows):
        """Create an unseen generic roster with a target near each end.

        Args:
            self: Fixture whose first source is replaced.
            rows: Number of complete name/topic records served by synthetic HTTP.

        Returns:
            None. Production HTML parsing and passage splitting remain unmodified.
        """
        self.last_name = f'Person{rows - 1:04d}'
        body = '<main><table><tr><th>Name</th><th>Topic</th></tr>'
        body += ''.join(f'<tr><td>Person{i:04d}</td><td>AI security</td></tr>' for i in range(rows))
        body += '</table></main>'
        self.flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'}, body.encode())

    def provider(self, request):
        """Supply early/late findings, empty middle batches and genuine review inputs.

        Args:
            self: Case recording source/passage call order and optional interruption.
            request: Serialized provider HTTP request from the production SDK.

        Returns:
            A provider-shaped response or a single abrupt SystemExit. A first-batch
            partial answer causes real production record refinements; no scheduler
            or parser is replaced. Advancing the clock sends no further requests.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        payload = json.loads(body['messages'][0]['content']) if title != 'ResearchPlan' else {}
        sid, passage = payload.get('source_id'), payload.get('passage', {})
        self.calls.append((title, sid, passage.get('passage_id')))
        if title == 'ExtractionBatch':
            pid = passage['passage_id']
            if sid == 'S1' and pid == self.crash_passage:
                self.crash_passage = None
                raise SystemExit(77)
            pairs = re.findall(r'Name: (Person\d+) \| Topic: (AI security)', passage['text'])
            pairs = [(name, topic) for name, topic in pairs
                     if name in {'Person0000', self.last_name} or pid.startswith('R')]
            entities, claims = [], []
            for i, (name, topic) in enumerate(pairs):
                person, subject = f'E{i}', f'T{i}'
                entities.extend([{'entity_id': person, 'name': name, 'entity_type': 'person'},
                                 {'entity_id': subject, 'name': topic, 'entity_type': 'topic'}])
                claims.append({'claim_id': f'C{i}', 'statement': f'{name} researches {topic}.',
                    'goal_ids': ['G1'], 'entity_ids': [person, subject], 'qualifications': [],
                    'research_roles': [self.role],
                    'relationship': {'subject_id': person, 'predicate': 'researches', 'object_id': subject},
                    'evidence': [{'passage_id': pid, 'quote': f'Name: {name} | Topic: {topic}'}]})
            if sid == 'S1' and pid == self.expire_after_passage:
                self.clock_offset = 3600
            return self.flow.cli.response(text=json.dumps({'entities': entities, 'claims': claims}))
        if title == 'ClaimReviewBatch':
            assessments = [{'candidate_id': c['candidate_id'], 'verdict': 'supported',
                'in_scope': True, 'rationale': 'Synthetic passage support for scheduling.',
                'report_mappings': fixture_mappings(c, payload), 'eligibility_assessments': fixture_eligibility(c, payload), 'evidence_quotes': [c['proposal']['evidence'][0]['quote']]} for c in payload['candidates']]
            return self.flow.cli.response(text=json.dumps({'assessments': assessments}))
        return self.base_provider(request)

    def search(self, query, max_results, monitor=None, **kwargs):
        """Capture the actual saved work state at every search boundary.

        Args:
            self: Fixture retaining immutable snapshot dictionaries.
            query: Application-selected search text.
            max_results: Requested result limit; zero results respects it.
            monitor: Unmodified production observer.
            **kwargs: Adapter options such as the configured search engine.

        Returns:
            An empty synthetic search result, without changing source eligibility.
        """
        _, saved = self.flow.cli.saved_session()
        self.search_snapshots.append(saved)
        return []

    def run_case(self, *flags):
        """Run the public command with real scheduling and bounded external fixtures.

        Args:
            self: Isolated source/model fixture.
            *flags: Additional genuine operator arguments overriding fixture defaults.

        Returns:
            Exit code, directory, saved session and terminal progress text.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            return self.flow.run_workflow('--search', '--max-search-queries', '1',
                '--explore-before-search-depth', '1', '--max-requests', '100', *flags)

    def test_finishes_productive_source_with_peer_probes_before_search(self):
        """Cross the old crawl checkpoint and retain the target at the document end.

        Args:
            self: Table with over twelve base passages and missing first-batch records.

        Returns:
            None. Assert real candidate persistence, completed refinements, batching,
            bounded peer probes, search ordering and document priority at the production entry.
        """
        _, directory, saved, progress = self.run_case('--max-no-gain-passages', '0')
        self.assertTrue(self.search_snapshots)
        first = self.search_snapshots[0]
        rows = [a for a in first['extraction']['attempts'] if a['source_id'] == 'S1']
        self.assertGreater(sum(not a['refinement_parent'] for a in rows), 12)
        self.assertTrue(any(a['refinement_parent'] for a in rows))
        self.assertTrue(all(a['status'] == 'completed' for a in rows))
        extraction_calls = [(sid, pid) for title, sid, pid in self.calls if title == 'ExtractionBatch']
        first_target = extraction_calls.index(('S1', 'P2'))
        source_calls = [sid for sid, _ in extraction_calls]
        # P1 contains only the table headings; focus starts when P2 yields a target.
        last_target = max(i for i, sid in enumerate(source_calls) if sid == 'S1')
        self.assertIn('S2', source_calls[first_target:last_target])
        self.assertEqual(source_calls.count('S1'), len(rows))
        self.assertTrue(any(self.last_name in c['proposal']['statement']
                            and c['adjudication_status'] == 'supported' for c in saved['extraction']['candidates']))
        self.assertTrue(all(c['candidate_id'] in first['claim_reviews']
                            for c in first['extraction']['candidates'] if c['source_id'] == 'S1'))
        self.assertLess(sum(title == 'ClaimReviewBatch' for title, _, _ in self.calls), 8)
        self.assertIn('continuing this document before more search', progress)
        self.assertEqual(saved['workflow']['document_focus']['release_reason'], 'all_passages_completed')
        events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['event'] == 'document_focus_changed' for e in events))

    def test_opted_in_completion_check_can_interrupt_productive_reading(self):
        """A long productive document must not starve opted-in completion assessment.

        Args:
            self: Long synthetic roster using the production scheduler and graph.
        Returns:
            None. Assert assessment occurs before all base passages finish, without
            asserting the synthetic model's completeness judgment is factual.
        """
        self.run_case('--stop-when-satisfied')
        first = next(i for i, call in enumerate(self.calls) if call[0] == 'CoverageAssessment')
        self.assertTrue(any(call[0] == 'ExtractionBatch' for call in self.calls[:first]))
        self.assertTrue(any(call[0] == 'ExtractionBatch' for call in self.calls[first + 1:]))
        self.assertLess(sum(c[0] == 'ExtractionBatch' and c[1] == 'S1' for c in self.calls[:first]), 12)

    def test_context_only_document_keeps_ordinary_exploration_checkpoint(self):
        """A name or background claim alone must not monopolize research.

        Args:
            self: Same long source with its claims explicitly classified as context.

        Returns:
            None. Assert search occurs with remaining passages and no document focus.
        """
        self.role = 'context'
        self.run_case()
        first = self.search_snapshots[0]
        self.assertNotIn('document_focus', first['workflow'])
        self.assertTrue(any(a['status'] == 'not_attempted' for a in first['extraction']['attempts']))

    def test_resume_preserves_focus_without_replaying_uncertain_passage(self):
        """A crashed call cannot erase the productive document's remaining work.

        Args:
            self: Fixture interrupted at the third base passage of the focused source.

        Returns:
            None. Assert one planner call, no duplicate extraction IDs, preserved
            interruption and late target extraction before any subsequent search.
        """
        self.crash_passage = 'P3'
        code, _, before, _ = self.run_case('--max-no-gain-passages', '0')
        self.assertEqual(code, 77)
        self.assertEqual(before['workflow']['document_focus']['source_id'], 'S1')
        with patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search), patch(
                'projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, saved = self.flow.resume(before['session_id'])
        calls = [(sid, pid) for title, sid, pid in self.calls if title == 'ExtractionBatch']
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(sum(title == 'ResearchPlan' for title, _, _ in self.calls), 1)
        self.assertEqual(next(a for a in saved['extraction']['attempts'] if a['batch_id'] == 'S1_P3')['error_code'],
                         'interrupted_outcome_unknown')
        self.assertTrue(any(self.last_name in c['proposal']['statement'] for c in saved['extraction']['candidates']))
        self.assertTrue(all(a['status'] != 'not_attempted' for a in self.search_snapshots[0]['extraction']['attempts']
                            if a['source_id'] == 'S1'))

    def test_deadline_stops_large_focused_queue_before_dispatching_more_passages(self):
        """A shared time limit must stop once, not drain hundreds of failed tasks.

        Args:
            self: Long table whose first successful extraction consumes the time allowance.

        Returns:
            None. Assert unsent work remains unattempted, with no search, review,
            additional provider requests or repeated extraction-node dispatches.
        """
        self.install_table(4800)
        self.expire_after_passage = 'P2'
        real_time, real_monotonic, observe = time.time, time.monotonic, RunMonitor.observe
        dispatched_after_deadline = []

        def watch(monitor, event, **details):
            """Bound regression runtime while recording real extraction-node entries.

            Args:
                monitor: Production RunMonitor instance whose behavior is retained.
                event: Actual graph event name.
                **details: Original whitelisted event fields.

            Returns:
                The original observer result; fail immediately on an expired dispatch
                rather than letting the historical failure storm run for minutes.
            """
            if self.clock_offset and event == 'node_started' and details.get('node_name') == 'extract_knowledge':
                dispatched_after_deadline.append(event)
                self.fail('Expired queue was dispatched again')
            return observe(monitor, event, **details)

        with patch('time.time', side_effect=lambda: real_time() + self.clock_offset), patch(
                'time.monotonic', side_effect=lambda: real_monotonic() + self.clock_offset), patch.object(
                    RunMonitor, 'observe', new=watch):
            _, _, saved, _ = self.run_case()
        self.assertEqual(dispatched_after_deadline, [])
        self.assertEqual(len(saved['model_attempts']), 4)
        self.assertEqual(saved['workflow']['stop_reason'], 'budget_exhausted')
        self.assertIn('run_time_limit', saved['workflow']['stop_details'])
        pending = [a for a in saved['extraction']['attempts'] if a['status'] == 'not_attempted']
        self.assertGreater(len(pending), 500)
        self.assertTrue(all(a['status'] in {'completed', 'not_attempted'} for a in saved['extraction']['attempts']))
        self.assertEqual(self.search_snapshots, [])

    def test_request_budget_preserves_unattempted_document_work(self):
        """Finishing a productive document never grants additional model allowance.

        Args:
            self: Productive table with fewer allowed calls than remaining passages.

        Returns:
            None. Assert the exact request ceiling and saved blocked focus/queue.
        """
        _, _, saved, _ = self.run_case('--max-requests', '8')
        self.assertEqual(len(saved['model_attempts']), 8)
        self.assertEqual(saved['workflow']['document_focus']['status'], 'blocked')
        self.assertTrue(any(a['status'] == 'not_attempted' for a in saved['extraction']['attempts']))
