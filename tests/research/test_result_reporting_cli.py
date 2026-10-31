"""Prove record repair and reporting through the production graph with offline transports.

These tests exercise the real parser, SDK schemas, scheduler, review, persistence
and terminal entry point. Handwritten replies prove control flow, not live-model
extraction accuracy. No benchmark answers or previous-run findings enter replies.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import hashlib
import json
import unittest
from unittest.mock import patch

from projectclarity.coverage.stopping import evidence_revision
from projectclarity.research_schemas import ResearchSession
from projectclarity.provisional_answer import read_saved_answer, render_provisional_answer
from projectclarity.terminal_run_summary import render_run_summary
from tests.research import test_workflow_control_cli as workflow_tests
from tests.retrieval.retrieval_test_fixtures import public_dns


class ResultReportingCLITests(unittest.TestCase):
    """Exercise malformed-but-retained structure and run-local result accounting."""

    def setUp(self):
        """Prepare independent source items, a shaped goal and synthetic HTTP routing.

        Args:
            self: Test owner; registers fixture cleanup with unittest.
        Returns:
            None. Only HTTP/DNS/model boundaries are replaced, not application nodes.
        """
        self.flow = workflow_tests.WorkflowControlCLITests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.cli = self.flow.cli
        self.fields = ['name', 'riding']
        self.items = [('Aster', 'North'), ('Birch', 'South')]
        self.unit = 'representatives'
        self.partial = True
        self.still_missing = False
        self.relational = True
        self.review_inputs = []
        self.extraction_inputs = []

    def statement(self, item):
        """Create the fixture's source sentence, not a model-derived expected answer.

        Args:
            self: Fixture selecting relational or timeline text.
            item: Pair of literal values written into the synthetic source.
        Returns:
            A unique quotation whose exact characters must survive real validation.
        """
        return f'{item[0]} represents {item[1]}.' if self.relational else f'{item[0]} occurred on {item[1]}.'

    def provider(self, request):
        """Return structure defects and repaired records through the actual SDK transport.

        Args:
            self: Fixture controlling only returned model content.
            request: Serialized provider request produced by the application adapter.
        Returns:
            Synthetic provider response. Unknown tasks fail instead of contacting a service.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        wire = json.loads(request.content)
        title = wire['output_config']['format']['schema']['title']
        if title == 'ResearchPlan':
            return self.cli.response()
        payload = json.loads(wire['messages'][0]['content'])
        if title == 'ExtractionBatch':
            self.extraction_inputs.append(payload)
            pid = payload['passage']['passage_id']
            damaged = self.partial and (not pid.startswith('R') or self.still_missing)
            entities, claims = [], []
            for index, item in enumerate(self.items):
                subject, obj = f'E{index}a', f'E{index}b'
                if self.relational:
                    entities.extend([{'entity_id': subject, 'name': item[0], 'entity_type': 'person'},
                        {'entity_id': obj, 'name': item[1], 'entity_type': 'place'}])
                bad = damaged and index == 0
                relationship = ({'subject_id': subject, 'predicate': 'represents', 'object_id': obj}
                    if self.relational and not bad else None)
                fields = [{'name': field, 'value': value} for field, value in zip(self.fields, item)]
                claims.append({'claim_id': f'C{index}', 'statement': self.statement(item),
                    'goal_ids': ['G1'], 'entity_ids': [subject, obj] if self.relational else [],
                    'relationship': relationship, 'claim_kind': 'relational' if self.relational else 'non_relational',
                    'result_records': [{'goal_id': 'G1', 'fields': fields[:1] if bad else fields}],
                    'qualifications': [], 'research_roles': ['answer_evidence'],
                    'evidence': [{'passage_id': pid, 'quote': self.statement(item)}]})
            return self.cli.response(text=json.dumps({'entities': entities, 'claims': claims}))
        if title == 'ClaimReviewBatch':
            self.review_inputs.append(payload)
            return self.cli.response(text=json.dumps({'assessments': [
                {'candidate_id': c['candidate_id'], 'verdict': 'supported', 'in_scope': True,
                 'rationale': 'Fixture values occur together in the original assigned passage.',
                 'report_mappings': fixture_mappings(c, payload), 'eligibility_assessments': fixture_eligibility(c, payload), 'evidence_quotes': [c['proposal']['evidence'][0]['quote']]}
                for c in payload['candidates']]}))
        raise AssertionError(title)

    def run_case(self, *, budget=10):
        """Invoke the real research command and locate only its newly created run.

        Args:
            self: Fixture defining the current source and planner response.
            budget: Total planning/extraction/review attempt allowance.
        Returns:
            Exit code, stdout, stderr, run directory, session and final answer JSON.
            No existing run is removed, overwritten or supplied to a new request.
        """
        self.cli.plan['goals'][0].update(question='List the source items and their associated values.',
            result_shape={'unit': self.unit, 'required_fields': self.fields})
        body = '<main>' + ''.join('<p>' + self.statement(item) + '</p>' for item in self.items) + '</main>'
        self.flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'}, body.encode())
        before = set(self.cli.output.glob('*/session.json'))
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            code, out, err = self.cli.run_cli(self.provider, '--extract',
                *(['--review-claims'] if budget >= 3 else []),
                '--max-requests', str(budget), '--question', 'List the source items and their associated values.',
                plan_only=False, details=False, source_dns=public_dns)
        paths = set(self.cli.output.glob('*/session.json')) - before
        self.assertEqual(len(paths), 1, err)
        directory = paths.pop().parent
        return code, out, err, directory, json.loads((directory / 'session.json').read_text()), json.loads((directory / 'answer.json').read_text())

    def test_repair_preserves_original_claims_and_counts_two_records_not_four_mentions(self):
        """Require bounded repair and full-field review on the production execution path.

        Args:
            self: Two source pairs, one initially missing both relationship and a field.
        Returns:
            None; asserts preserved receipts, exact record counts, quotes and progress output.
        """
        code, out, err, _, saved, answer = self.run_case()
        self.assertEqual(code, 0, err)
        attempts = saved['extraction']['attempts']
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1]['refinement_parent'], attempts[0]['batch_id'])
        self.assertIsNone(attempts[0]['result']['claims'][0]['relationship'])
        self.assertEqual(len(saved['extraction']['candidates']), 4)
        results = answer['progress']['goals'][0]['results']
        self.assertEqual(results['supported'], 2)
        self.assertEqual(results['incomplete'], 1)
        self.assertEqual(len(results['records']), 3)
        self.assertTrue(all(c['proposal']['result_records'] for p in self.review_inputs for c in p['candidates']))
        self.assertEqual(self.extraction_inputs[0]['goals'][0]['result_shape']['required_fields'], self.fields)
        self.assertNotIn('refinement', self.extraction_inputs[0])
        hint = self.extraction_inputs[1]['refinement']['issues'][0]
        self.assertEqual(hint['source_quote'], 'Aster represents North.')
        self.assertIn('incomplete_relationship', hint['detected_issues'])
        self.assertIn('incomplete_result_record', hint['detected_issues'])
        self.assertIn('representatives: 2 identified; 2 supported complete records', out)
        self.assertIn('representatives: 2 identified; 2 supported complete records', err)
        self.assertIn('Next: targeted follow-up to S1_P1', err)
        self.assertIn('Discovered candidate: Aster represents North.', err)
        self.assertIn('Supported finding: Birch represents South.', err)
        self.assertEqual(err.count('Budget · retrieval wall time:'), 2)
        self.assertLessEqual(saved['usage']['requests_attempted'], 5)

    def test_persistent_defect_stays_visible_without_recursive_calls(self):
        """Retain unresolved structure when the single follow-up also fails to supply it.

        Args:
            self: Two source items with an unrepaired first pair.
        Returns:
            None; asserts exactly two extraction attempts and no falsely complete pair.
        """
        self.still_missing = True
        _, out, _, _, saved, answer = self.run_case()
        self.assertEqual(len(saved['extraction']['attempts']), 2)
        results = answer['progress']['goals'][0]['results']
        self.assertEqual((results['supported'], results['incomplete']), (1, 2))
        self.assertIn('Incomplete structure:', out)
        self.assertIn('Aster represents North.', out)
        self.assertEqual(len(saved['extraction']['candidates']), 4)

    def test_budget_stop_retains_unattempted_repair_and_pending_results(self):
        """A repair may not bypass the user's global model budget or manufacture support.

        Args:
            self: Budget sufficient only for planning and initial extraction, without review.
        Returns:
            None; asserts explicit pending/incomplete records and an unattempted child.
        """
        code, _, _, _, saved, answer = self.run_case(budget=2)
        self.assertEqual(code, 1)
        self.assertEqual(saved['usage']['requests_attempted'], 2)
        self.assertEqual([a['status'] for a in saved['extraction']['attempts']], ['completed', 'not_attempted'])
        results = answer['progress']['goals'][0]['results']
        self.assertEqual((results['supported'], results['pending'], results['incomplete']), (0, 1, 1))

    def test_timeline_result_fields_are_not_hardcoded_to_people_or_relationships(self):
        """Use event/date results with non-relational claims and no invented entity edges.

        Args:
            self: A finite timeline fixture defining its own fields and unit.
        Returns:
            None; asserts complete results without a needless repair request.
        """
        self.relational, self.partial = False, False
        self.fields, self.unit = ['event', 'date'], 'events'
        self.items = [('Gate opening', '2020-01-01'), ('Bell ringing', '2020-01-02')]
        _, out, _, _, saved, answer = self.run_case()
        self.assertEqual(len(saved['extraction']['attempts']), 1)
        self.assertEqual(answer['progress']['goals'][0]['results']['supported'], 2)
        self.assertIn('events: 2 identified; 2 supported complete records', out)
        self.assertEqual(saved['usage']['requests_attempted'], 3)

    def test_resume_does_not_repeat_completed_repair_or_reset_budget(self):
        """Reload the actual journal after a completed repair without repeating model work.

        Args:
            self: Fully processed production fixture with one completed follow-up.
        Returns:
            None; request count, child IDs and original incomplete proposals survive resume.
        """
        _, _, _, _, saved, _ = self.run_case()
        self.flow.provider = self.provider
        code, resumed = self.flow.resume(saved['session_id'])
        self.assertEqual(code, 0)
        self.assertEqual(resumed['usage']['requests_attempted'], saved['usage']['requests_attempted'])
        self.assertEqual([a['batch_id'] for a in resumed['extraction']['attempts']],
                         [a['batch_id'] for a in saved['extraction']['attempts']])
        self.assertIsNone(resumed['extraction']['attempts'][0]['result']['claims'][0]['relationship'])

    def test_new_run_cannot_read_prior_findings_even_in_the_same_output_root(self):
        """Leave an entire old run and shared checkpoint in place while starting afresh.

        Args:
            self: Two runs using the same URL but disjoint source contents.
        Returns:
            None; asserts independent IDs, model inputs, receipts and result inventories.
        """
        self.partial = False
        self.items = [('PRIOR_SENTINEL_PERSON', 'PRIOR_SENTINEL_PLACE')]
        _, _, _, old, prior, _ = self.run_case()
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob('*') if p.is_file()}
        boundary = len(self.cli.requests)
        self.items = [('New Person', 'New Place')]
        _, out, err, _, current, answer = self.run_case()
        self.assertNotEqual(current['session_id'], prior['session_id'])
        self.assertEqual(current['usage']['requests_attempted'], 3)
        self.assertEqual(answer['progress']['goals'][0]['results']['supported'], 1)
        for request in self.cli.requests[boundary:]:
            self.assertNotIn('PRIOR_SENTINEL', request.content.decode())
        self.assertNotIn('PRIOR_SENTINEL', json.dumps(current) + out + err)
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob('*') if p.is_file()})

    def test_final_reporting_marks_stale_and_legacy_assessments_without_extra_calls(self):
        """Exercise the saved-run reporting path with stale, unknown and current fingerprints.

        Args:
            self: Completed production run; modifies only this temporary fixture snapshot.
        Returns:
            None; historical gaps stay out of current conclusions, evidence remains visible,
            and all inspection calls leave source/run files unchanged.
        """
        self.partial = False
        _, _, _, directory, saved, _ = self.run_case()
        session = ResearchSession.model_validate(saved)
        session.coverage = {'assessment_status': 'assessed', 'history': [
            {'round': 1, 'candidate_total': 1, 'evidence_revision': 'earlier-evidence', 'offered_candidate_ids': []}],
            'criteria_progress': [{'criterion_id': 'C1', 'status': 'partial', 'observed_count': 1,
                'gaps': ['Historical count: only one result.']}]}
        for expected in ('stale', 'freshness_unknown', 'assessed'):
            if expected == 'freshness_unknown':
                session.coverage['history'][0].pop('evidence_revision')
            elif expected == 'assessed':
                session.coverage['history'][0]['evidence_revision'] = evidence_revision(session)
            (directory / 'session.json').write_text(session.model_dump_json())
            before = (directory / 'session.json').read_bytes()
            inspected = read_saved_answer(self.cli.output, session.session_id)
            rendered = render_provisional_answer(inspected)
            summary = render_run_summary(session, self.cli.output)
            self.assertEqual(inspected['coverage'], expected)
            self.assertIn('Aster represents North.', rendered)
            if expected != 'assessed':
                self.assertIn('OUTDATED', rendered)
                self.assertNotIn('Historical count: only one result.', rendered)
                self.assertNotIn('Historical count: only one result.', summary)
                self.assertIn('Historical assessment gaps', render_provisional_answer(inspected, include_leads=True))
            self.assertEqual(before, (directory / 'session.json').read_bytes())
        self.assertEqual(len(self.cli.requests), 3)
