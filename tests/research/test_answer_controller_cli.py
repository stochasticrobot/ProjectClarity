"""Check the designed-answer reading/review/discovery cycle under competing work.

The public command, workflow and scheduler run with synthetic sources and provider
responses. Cases vary source order, introduce continuing search suggestions and
scope drift, interrupt reading and exercise explicit source rejection. Assertions
check that useful acquired documents still receive reading turns and that saved
service counts survive restart. They do not certify live model relevance judgments.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings
import json
import random
import re
import unittest
from unittest.mock import patch

from projectclarity.discovery.search_provider import SearchProviderError
from projectclarity.research_schemas import ResearchSession
from projectclarity.coverage.stopping import completion_status
from tests.research import test_workflow_control_cli as control
from tests.research.test_contract_completion import explicit_specification


class AnswerControllerCLITests(unittest.TestCase):
    """Exercise sustained local progress, discovery, explicit scope and restart together."""

    def setUp(self):
        """Create source-derived fixtures without replacing any scheduling node.

        Args:
            self: Test owner of temporary files, SDK and HTTP boundary fixtures.
        Returns:
            None. Unknown URLs and actual network connections remain test failures.
        """
        self.flow = control.WorkflowControlCLITests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.cli = self.flow.cli
        self.flow.provider = self.provider
        self.flow.search = self.search
        self.count = 104
        self.schema = explicit_specification(self.count, supporting=True)
        self.cli.plan['goals'][1]['requirement_ids'] = ['A2']
        self.cli.plan['strategy'][0]['proposed_searches'] = ['collection evidence']
        self.cli.plan['clarifications'] = []
        self.events, self.snapshots, self.inputs = [], [], []
        self.crash_pid = None
        self.empty_primary = False
        self.search_failure = False
        self.study = False
        self.install_sources(19)

    def install_sources(self, seed, reverse=False):
        """Construct unseen row order and optionally reverse source registration order.

        Args:
            self: Fixture holding the source routes and explicit answer contract.
            seed: Deterministic permutation seed, unrelated to result identity.
            reverse: Whether the supporting source is registered before the roster.
        Returns:
            None. Writes fixture inputs only; production parsing chooses passages.
        """
        items = list(range(self.count))
        random.Random(seed).shuffle(items)
        body = '<main><table><tr><th>Item</th><th>Value</th></tr>'
        body += ''.join(f'<tr><td>Entry{i:04d}</td><td>Value{i*17+3}</td></tr>' for i in items)
        body += '<tr><td>HistoricalOnly</td><td>PastValue</td></tr></table>'
        body += '<a href="/unrelated-link">Unrelated sports results</a></main>'
        self.flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'}, body.encode())
        self.flow.routes[('example.org', '/other')] = (200, {'Content-Type': 'text/plain'},
            b'This collection covers the requested scope.')
        urls = ['https://example.org/research', 'https://example.org/other']
        self.cli.urls.write_text('\n'.join(reversed(urls) if reverse else urls) + '\n')
        self.primary = 'S2' if reverse else 'S1'

    def provider(self, request):
        """Supply adversarial ongoing gaps and source-derived facts at the SDK boundary.

        Args:
            self: Fixture recording actual dispatched inputs and optional crash.
            request: Real serialized SDK request with its model response schema.
        Returns:
            Synthetic provider response, or one abrupt interruption after reservation.
            No application controller, validation or evidence projection is mocked.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        wire = json.loads(request.content)
        title = wire['output_config']['format']['schema']['title']
        payload = json.loads(wire['messages'][0]['content'])
        self.inputs.append((title, payload))
        sid, passage = payload.get('source_id'), payload.get('passage', {})
        self.events.append((title, sid, passage.get('passage_id')))
        if title == 'AnswerSchema':
            answer = self.schema
        elif title == 'ResearchPlan':
            return self.cli.response()
        elif title == 'CoverageStudyPlan':
            answer = dict(needed=True, reason='Check the collection scope.',
                plan=['Inspect a bounded source sample.'], source_ids=[],
                queries=[dict(query='collection scope overview', goal_ids=['G2'], reason='Assess collection scope.')],
                readiness=dict(answer_type='records', evidence_scope='mixed', numeric_estimate_applicable=False,
                    coverage_checks=[], unresolved_questions=[]))
        elif title == 'ContractStudySelection':
            answer = dict(selections=[dict(candidate_id=row['candidate_id'],
                goal_ids=['G2'], requirement_ids=[] if 'unrelated' in row.get('url','') else ['A2'],
                disposition='reject' if 'unrelated' in row.get('url','') else 'specific_lead',
                reason='Unrelated source.' if 'unrelated' in row.get('url','') else 'Collection scope probe.')
                for row in payload['candidates']])
        elif title == 'CoverageStudyAssessment':
            answer = dict(summary='Bounded fixture scope probe.', coverage_boundary='Requested collection.',
                coverage_outcome='established', readiness=dict(answer_type='records', evidence_scope='mixed',
                    numeric_estimate_applicable=False, coverage_checks=[], unresolved_questions=[]),
                estimates=[], limitations=['A method, not completed research.'])
        elif title == 'ExtractionBatch':
            if sid == self.primary and passage['passage_id'] == self.crash_pid:
                self.crash_pid = None
                raise SystemExit(77)
            claims = []
            pairs = re.findall(r'Item: (Entry\d+|HistoricalOnly) \| Value: (\w+)', passage['text'])
            if self.empty_primary and sid == self.primary:
                pairs = []
            for i, (item, value) in enumerate(pairs):
                quote = f'Item: {item} | Value: {value}'
                claims.append(dict(claim_id=f'C{i}', statement=quote, goal_ids=['G1'],
                    entity_ids=[], relationship=None, claim_kind='non_relational', qualifications=[],
                    research_roles=['lead'] if item == 'HistoricalOnly' else ['answer_evidence'],
                    result_records=[dict(goal_id='G1', fields=[dict(name='item', value=item), dict(name='value', value=value)])],
                    evidence=[dict(passage_id=passage['passage_id'], quote=quote)]))
            scope = 'This collection covers the requested scope.'
            if scope in passage['text']:
                claims.append(dict(claim_id='Scope', statement=scope, goal_ids=['G2'], entity_ids=[],
                    relationship=None, claim_kind='non_relational', qualifications=[],
                    research_roles=['coverage_evidence'], result_records=[],
                    evidence=[dict(passage_id=passage['passage_id'], quote=scope)]))
            answer = dict(entities=[], claims=claims)
        elif title == 'ClaimReviewBatch':
            answer = dict(assessments=[dict(candidate_id=c['candidate_id'], verdict='supported', in_scope=True,
                rationale='Fixture passage directly contains this qualified statement.',
                report_mappings=fixture_mappings(c, payload), evidence_quotes=[c['proposal']['evidence'][0]['quote']]) for c in payload['candidates']])
        elif title == 'CoverageAssessment':
            supported = payload['result_inventory']['G1']['supported']
            assessments = []
            for criterion in self.schema['completion_checks']:
                auxiliary = criterion['criterion_id'] == 'A2'
                ids = [c['candidate_id'] for c in payload['candidates']
                       if ('G2' in c['goal_ids'] if auxiliary else 'answer_evidence' in c['roles'])]
                satisfied = bool(ids) if auxiliary else supported == self.count
                assessments.append(dict(criterion_id=criterion['criterion_id'],
                    judgment='satisfied' if satisfied else 'partial', items=[],
                    target_count=criterion['target_count'], result_goal_id=criterion['result_goal_id'],
                    benchmark_candidate_ids=[], evidence_candidate_ids=ids,
                    gaps=[] if satisfied else ['Requested entries still missing.'],
                    rationale='Controlled assessment against explicit requirements, not source count.'))
            answer = dict(assessments=assessments, scope_estimates=[], next_searches=[dict(
                query=f'additional independent collection check {len(self.inputs)}', goal_ids=['G1'],
                reason='Continuing adversarial gap suggestions must not starve acquired work.')])
        elif title in {'ContractSearchSelection', 'ContractFollowupPlan'}:
            search = title == 'ContractSearchSelection'
            key = 'result_id' if search else 'link_id'
            choices = []
            for row in payload['results' if search else 'links']:
                useful = '/lead' in row['url']
                choices.append({key: row[key], 'goal_ids': ['G2' if useful else 'G1'],
                    'requirement_ids': ['A2'] if useful else [],
                    'disposition': 'specific_lead' if useful else 'reject',
                    'reason': 'Specific collection-scope lead.' if useful else 'Unrelated; do not acquire.'})
            answer = dict(selections=choices)
            if not search:
                answer.update(gaps=['Scope corroboration.'], stop_reason='Evaluate observed links.')
        else:
            raise AssertionError(title)
        return self.cli.response(text=json.dumps(answer))

    def search(self, query, max_results, monitor=None, **kwargs):
        """Return mixed fallback-like results while preserving each discovery boundary.

        Args:
            self: Fixture holding saved-state snapshots and admitted source routes.
            query: Actual queued query selected by production code.
            max_results: Normal result capacity, respected by the fixture.
            monitor: Production observer; never changed.
            **kwargs: Normal search adapter options, unused by the offline transport.
        Returns:
            Useful plus explicitly unrelated metadata, optionally failing the second
            query when the fault scenario enables search_failure. No claim evidence enters through this search response.
        """
        self.snapshots.append(self.cli.saved_session()[1])
        self.events.append(('search', None, None))
        turn = len(self.snapshots)
        if turn == 2 and self.search_failure:
            raise SearchProviderError('search_provider_blocked')
        path = f'/lead{turn}'
        self.flow.routes[('example.org', path)] = (200, {'Content-Type': 'text/plain'},
            b'This collection covers the requested scope.')
        return [dict(url='https://example.org/unrelated-country', title='Unrelated country', snippet='Not the requested population.'),
                dict(url='https://example.org'+path, title='Collection scope', snippet='Specific scope evidence.')][:max_results]

    def run_case(self, budget=100):
        """Invoke the genuine terminal entry and graph under transport-only fixtures.

        Args:
            self: Fixture with registered source inputs and model replies.
            budget: Ordinary shared model allowance; no test-only controller settings.
        Returns:
            Terminal outcome, run folder, saved state and progress output.
        """
        with (patch('projectclarity.retrieval.retrieval_limits.time.sleep'),
              patch('projectclarity.nodes.schedule_work.workflow_control.choose_legacy_work',
                    side_effect=AssertionError('Versioned run entered legacy arbitration'))):
            return self.flow.run_workflow('--design-answer', '--assess-coverage', '--stop-when-satisfied',
                *(['--coverage-study'] if self.study else []),
                '--search', '--max-search-queries', '20', '--max-requests', str(budget), '--model-concurrency', '1')

    def test_productive_reading_survives_continuing_gaps_and_scope_drift(self):
        """Verify combined failure invariants on two unseen source/row orderings.

        Args:
            self: Production-entry fixture, not a helper-only scheduling simulation.
        Returns:
            None; proves sustained reads, explicit rejection, auxiliary evidence,
            preserved historical leads, contract stopping and unused budget.
        """
        for seed, reverse in [(19, False), (83, True)]:
            with self.subTest(seed=seed, reverse=reverse):
                case = AnswerControllerCLITests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.install_sources(seed, reverse)
                code, directory, saved, err = case.run_case()
                self.assertEqual(code, 0, err)
                self.assertEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
                self.assertLess(saved['usage']['requests_attempted'], 100)
                self.assertGreaterEqual(len(case.snapshots), 2)
                rounds = {s['search']['round']: s for s in case.snapshots}
                self.assertGreaterEqual(len(rounds), 2)
                primary_counts = [sum(a['source_id'] == case.primary and a['status'] == 'completed'
                    for a in s['extraction']['attempts']) for s in rounds.values()]
                self.assertGreaterEqual(primary_counts[0], 3)
                self.assertTrue(all(b > a for a, b in zip(primary_counts, primary_counts[1:])), primary_counts)
                self.assertFalse(any('unrelated' in path for _, path, _ in case.flow.http.requests))
                selections = saved['search']['selections']
                self.assertTrue(any(i['status'] == 'declined' for s in selections for i in s.get('items', [])))
                report = json.loads((directory/'answer.json').read_text())['progress']['goals'][0]['results']
                self.assertEqual(report['supported'], case.count)
                self.assertFalse(any(r['values'].get('item') == 'HistoricalOnly' for r in report['records']))
                self.assertTrue(any('HistoricalOnly' in c['proposal']['statement'] for c in saved['extraction']['candidates']))
                supporting = next(c for c in saved['coverage']['criteria_progress'] if c['criterion_id'] == 'A2')
                self.assertEqual(supporting['cross_goal_candidate_ids'], [])
                self.assertTrue(supporting['evidence_candidate_ids'])
                coverage = [p for title,p in case.inputs if title == 'CoverageAssessment']
                self.assertTrue(any(r['unread_passages'] > 0
                    and r['source_context']['url'] == 'https://example.org/research'
                    and r['source_context']['acquired_at']
                    for p in coverage for r in p['acquired_work']))
                self.assertNotIn('document_focus', saved['workflow'])
                self.assertNotIn('exploration_cycle', saved['workflow'])
                self.assertTrue(completion_status(ResearchSession.model_validate(saved), case.cli.output)['ready'])

    def test_dry_source_and_uncertain_read_do_not_reset_or_replay_service(self):
        """Check early yielding and interrupted extraction through real resume.

        Args:
            self: Fixture combining zero-yield passages, recurring gaps and a crash.
        Returns:
            None; uncertain work is not paid for twice, independent work proceeds,
            and incomplete evidence cannot pass the contract despite available leads.
        """
        self.empty_primary = True
        self.search_failure = True
        self.crash_pid = 'P3'
        code, directory, before, _ = self.run_case(budget=30)
        self.assertEqual(code, 77)
        saved_controller = before['workflow']['answer_controller'].copy()
        with (patch('projectclarity.nodes.web_search.node.search_web', side_effect=self.search),
              patch('projectclarity.retrieval.retrieval_limits.time.sleep')):
            code, saved = self.flow.resume(before['session_id'])
        self.assertIn(code, (0,1))
        self.assertEqual(self.events.count(('ExtractionBatch', self.primary, 'P3')), 1)
        self.assertGreaterEqual(saved['workflow']['answer_controller']['cycle'], saved_controller['cycle'])
        self.assertLessEqual(saved['usage']['requests_attempted'], 30)
        self.assertGreaterEqual(saved['usage']['requests_attempted'], before['usage']['requests_attempted'])
        self.assertTrue(self.snapshots)
        first_reads = sum(a['source_id'] == self.primary and a['status'] == 'completed'
                          for a in self.snapshots[0]['extraction']['attempts'])
        self.assertLessEqual(first_reads, 4)
        self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
        self.assertTrue(any(c['source_id'] != self.primary for c in saved['extraction']['candidates']))

    def test_preliminary_and_followup_selection_share_explicit_rejection(self):
        """Exercise both non-search selection adapters through the main entry point.

        Args:
            self: Short-population fixture intentionally unable to meet its target.
        Returns:
            None; preliminary and link rejections do not acquire unrelated URLs,
            and the unmet answer target remains blocked despite positive judgments.
        """
        self.count = 8
        self.schema = explicit_specification(9, supporting=True)
        self.study = True
        self.install_sources(41)
        code, directory, saved, _ = self.run_case(budget=25)
        self.assertIn(code, (0,1))
        titles = [title for title, _ in self.inputs]
        self.assertIn('ContractStudySelection', titles)
        self.assertIn('ContractFollowupPlan', titles)
        self.assertTrue(any(i['status'] == 'declined' for i in saved['coverage_study']['selection']['items']))
        self.assertFalse(any('unrelated' in path for _, path, _ in self.flow.http.requests))
        self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
