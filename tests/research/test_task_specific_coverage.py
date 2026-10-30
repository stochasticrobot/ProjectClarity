"""Check evidence requirements for answer types that do not all have a countable population.

Synthetic source and model responses drive the public command, workflow and SDK.
Tests inspect how the preliminary study's proposed checks reach later assessments,
how statuses are saved and how invalid references are rejected. An explanation,
comparison or timeline must not require an invented population count merely to
represent progress. These are validation and data-delivery tests, not live reasoning scores.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings
import json
import unittest

from tests.research import test_early_coverage_study as early


class TaskSpecificCoverageTests(unittest.TestCase):
    """Separate coverage-method establishment, numerical estimates and answer progress."""

    def setUp(self):
        """Create a network-forbidden harness with one goal and overridable task data.

        Args:
            self: Case owning isolated source files and saved-run artifacts.
        Returns:
            None. Install synthetic transports and configure a general evidence criterion.
        """
        self.flow = early.EarlyCoverageStudyTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.flow.cli.plan['goals'] = [{'goal_id': 'G1', 'question': 'Answer the supplied question.',
            'evidence_required': ['Relevant, attributable source passages.']}]
        self.flow.cli.plan['completion_criteria'] = [{'criterion_id': 'K1', 'goal_ids': ['G1'],
            'description': 'Support the answer with relevant evidence.', 'kind': 'evidence_sufficiency',
            'measurement_method': 'Check the offered evidence.', 'target_basis': 'not_applicable',
            'target_count': None, 'limitations': []}]
        self.flow.cli.plan['strategy'][0]['goal_ids'] = ['G1']
        self.flow.cli.plan['strategy'][0]['proposed_searches'] = []
        self.flow.cli.plan['scope_in'] = ['The supplied task and its evidence boundary.']
        self.flow.cli.plan['scope_out'] = ['Unsupported conclusions.']
        self.flow.cli.plan['assumptions'] = []
        self.flow.readiness['coverage_checks'][0]['goal_ids'] = ['G1']
        self.flow.no_estimates = True
        self.flow.seed_ids = ['S1']
        self.question = 'Explain the supplied material.'
        self.quote = 'This synthetic source supplies a relevant fact.'
        self.extra_fault = None
        self.evidence_requirements = ['Relevant passages from the supplied document.']
        self.coverage_inputs = []
        self.baseline_provider = self.flow.provider
        self.flow.provider = self.provider

    def provider(self, request):
        """Provide anchored facts, real claim reviews and criterion-linked judgments.

        Args:
            self: Case selecting the task form and optional readiness-reference failure.
            request: Actual provider-wire request from the production SDK.
        Returns:
            A synthetic response through the real parser. Other study/planning requests
            use the existing fixture; no application node or validator is replaced.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        if title not in {'ExtractionBatch', 'ClaimReviewBatch', 'CoverageAssessment'}:
            return self.baseline_provider(request)
        payload = json.loads(body['messages'][0]['content'])
        self.flow.schemas.append(title)
        self.flow.payloads.append((title, payload))
        if title == 'ExtractionBatch':
            answer = {'entities': [], 'claims': [{'claim_id': 'C1', 'statement': self.quote,
                'goal_ids': ['G1'], 'entity_ids': [], 'relationship': None, 'qualifications': [],
                'evidence': [{'passage_id': payload['passage']['passage_id'], 'quote': self.quote}]}]}
        elif title == 'ClaimReviewBatch':
            answer = {'assessments': [{'candidate_id': c['candidate_id'], 'verdict': 'supported',
                'in_scope': True, 'rationale': 'The synthetic source states this fact.',
                'report_mappings': fixture_mappings(c, payload), 'evidence_quotes': [self.quote]} for c in payload['candidates']]}
        else:
            self.coverage_inputs.append(payload)
            ids = [c['candidate_id'] for c in payload['candidates']]
            rows = []
            for c in payload['criteria']:
                refs = ['invented_claim'] if self.extra_fault == 'unknown_evidence' and c['criterion_id'] != 'K1' else ids
                rows.append({'criterion_id': c['criterion_id'], 'judgment': 'satisfied',
                    'items': [], 'target_count': None, 'benchmark_candidate_ids': [],
                    'evidence_candidate_ids': refs, 'gaps': [], 'rationale': 'Synthetic evidence suffices for this check.'})
            answer = {'assessments': rows, 'next_searches': []}
        return self.flow.cli.response(text=json.dumps(answer))

    def run_document_task(self, answer_type, description, method, *flags):
        """Run a complete local-material study and recursive assessment for one task shape.

        Args:
            self: Case containing question text and an exact synthetic source quotation.
            answer_type: Descriptive answer form delivered to the real model contract.
            description: Coverage requirement specific to this question.
            method: Concrete way the coverage model can assess the requirement.
            *flags: Additional terminal options for failure/budget/permission tests.
        Returns:
            The standard saved-run tuple after the actual production entry point returns.
            No URL seeds or external source downloads are permitted in this invocation.
        """
        document = self.flow.cli.directory / 'source.txt'
        document.write_text(self.quote + '\n')
        self.flow.cli.plan['objective'] = self.question
        self.flow.readiness.update(answer_type=answer_type, evidence_scope='provided_material',
            numeric_estimate_applicable=False)
        self.flow.readiness['coverage_checks'][0].update(description=description,
            assessment_method=method, evidence_required=self.evidence_requirements)
        return self.flow.run_case('--question', self.question, '--document', str(document),
            '--extract', '--review-claims', *flags, include_urls=False)

    def assert_tracked_method(self, out, saved, answer_type):
        """Check actual assessed progress without treating method establishment as completion.

        Args:
            self: Case retaining actual serialized coverage requests.
            out: Terminal output captured from the production entry point.
            saved: Persisted session with study and main-loop coverage artifacts.
            answer_type: Expected task form, not a hard-coded application taxonomy.
        Returns:
            None. Assert successful non-numeric method, separately assessed checks,
            unchanged original criteria, no automatic stop and no source network calls.
        """
        self.assertEqual(saved['coverage_study']['coverage_outcome'], 'established')
        self.assertEqual(saved['coverage_study']['estimate_outcome'], 'not_applicable')
        self.assertEqual(saved['coverage_study']['readiness']['answer_type'], answer_type)
        self.assertEqual(saved['coverage_study']['estimates'], [])
        self.assertEqual(saved['coverage']['readiness_progress'][0]['status'], 'model_assessed_satisfied')
        self.assertEqual([c['criterion_id'] for c in saved['coverage']['criteria_progress']], ['K1'])
        self.assertEqual(len(saved['plan']['completion_criteria']), 1)
        self.assertFalse(saved['request']['stop_when_satisfied'])
        self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
        self.assertFalse(self.flow.http.requests)
        self.assertFalse(self.flow.searches)
        self.assertIn('not applicable to this task (not a failure)', out)
        self.assertIn('Coverage method: established', out)
        self.assertIn('CS1 [model_assessed_satisfied]', out)
        self.assertTrue(self.coverage_inputs)
        self.assertEqual({c['criterion_id'] for c in self.coverage_inputs[-1]['criteria']}, {'K1', 'CS1'})

    def test_automatic_stop_requires_scope_study_checks(self):
        """Exercise all-check stopping and rejection through the real research graph.

        Args:
            self: Transport-only fixture with source-backed nonnumeric evidence.
        Returns:
            None. Assert a valid primary judgment cannot hide invalid supplemental
            evidence; a fresh independent valid run can stop on both sets of checks.
        """
        self.extra_fault = 'unknown_evidence'
        code, _, err, _, saved = self.run_document_task('explanation',
            'Check source attribution.', 'Check the quoted passage.', '--stop-when-satisfied')
        self.assertEqual(code, 0, err)
        self.assertEqual(saved['coverage']['criteria_progress'][0]['status'], 'model_assessed_satisfied')
        self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
        # Each harness owns exactly one run; use a fresh output root for the
        # independent success case rather than mixing two session directories.
        self.setUp()
        code, _, err, _, saved = self.run_document_task('explanation',
            'Check source attribution.', 'Check the quoted passage.', '--stop-when-satisfied')
        self.assertEqual(code, 0, err)
        self.assertEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')

    def test_book_explanation_has_non_numeric_checks_and_no_web_dependency(self):
        """An explanation task can establish coverage without inventing a population size.

        Args:
            self: Case using a synthetic mystery passage, not a real book conclusion.
        Returns:
            None. Assert the production path handles suspect evidence as a qualitative check.
        """
        self.question = 'Who committed the crime in this book?'
        self.quote = 'In this synthetic mystery, the final chapter reveals that Mara committed the crime.'
        code, out, err, _, saved = self.run_document_task('explanation',
            'Reconcile the revelation with suspect evidence and contradictions.',
            'Check attribution and competing explanations against relevant passages.')
        self.assertEqual(code, 0, err)
        self.assert_tracked_method(out, saved, 'explanation')

    def test_timeline_has_ordering_and_uncertainty_checks(self):
        """Chronology requires ordering evidence rather than a guessed entity denominator.

        Args:
            self: Case providing explicit ordering in an invented local document.
        Returns:
            None. Assert a timeline-specific coverage requirement reaches main assessment.
        """
        self.question = 'Devise a timeline of events from the supplied document.'
        self.quote = 'The synthetic record states that the arrival happened before the meeting, whose date is unknown.'
        code, out, err, _, saved = self.run_document_task('timeline',
            'Anchor events and preserve unknown dates or contradictory ordering.',
            'Compare event references and distinguish dates from relative ordering.')
        self.assertEqual(code, 0, err)
        self.assert_tracked_method(out, saved, 'timeline')

    def test_sales_comparison_tracks_metric_period_and_comparability(self):
        """Ranking support is not measured by counting discovered people or organizations.

        Args:
            self: Case using fictional Ontario SUV sales values in a supplied report.
        Returns:
            None. Assert the task-specific comparison method passes through all real nodes.
        """
        self.question = 'Find the top selling brand of SUV in Ontario using this report.'
        self.quote = 'The synthetic report ranks Brand A above Brand B by Ontario SUV unit sales in 2025.'
        code, out, err, _, saved = self.run_document_task('comparison/ranking',
            'Compare all relevant brands on Ontario SUV unit sales for the same period.',
            'Check geographic coverage, vehicle category, metric, period and comparable competitors.')
        self.assertEqual(code, 0, err)
        self.assert_tracked_method(out, saved, 'comparison/ranking')

    def test_finite_roster_can_retain_a_source_grounded_numeric_estimate(self):
        """A finite enumeration can use a cited total without treating it as already complete.

        Args:
            self: Case with an explicitly fictional three-member roster and dated boundary.
        Returns:
            None. Assert a numeric estimate stays separate from criteria and candidate facts.
        """
        self.question = 'List all serving members of Canadian Parliament from the supplied snapshot.'
        self.quote = 'This synthetic roster contains three serving members as of the snapshot date.'
        self.flow.readiness.update(answer_type='enumeration', evidence_scope='provided_material')
        self.flow.readiness['coverage_checks'][0].update(description='Reconcile members and vacancies with the dated roster.',
            kind='enumeration', assessment_method='Match each roster entry and account for every seat.')
        self.flow.no_estimates = False
        self.flow.estimate_override = {'unit': 'members in the synthetic snapshot',
            'range': {'lower': 3, 'upper': 3}, 'evidence': [{'source_id': 'S1', 'quote': self.quote}]}
        document = self.flow.cli.directory / 'roster.txt';document.write_text(self.quote)
        code, _, err, _, saved = self.flow.run_case('--question', self.question,
            '--document', str(document), '--extract', '--review-claims', '--assess-coverage', include_urls=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(saved['coverage_study']['estimates'][0]['range'], {'lower': 3, 'upper': 3})
        self.assertEqual(saved['coverage_study']['coverage_outcome'], 'established')
        self.assertIsNone(saved['plan']['completion_criteria'][0]['target_count'])
        self.assertEqual(len(saved['extraction']['candidates']), 1)
        self.assertEqual(saved['coverage']['readiness_progress'][0]['status'], 'partial')
        self.assertIn('unknown_or_unmatched_target', saved['coverage']['readiness_progress'][0]['blocking_reasons'])
        self.assertFalse(self.flow.searches)

    def test_invalid_optional_readiness_does_not_erase_primary_progress(self):
        """A bad supplemental evidence ID must not invalidate sound original criteria.

        Args:
            self: Case returning an invented reference only for the supplemental check.
        Returns:
            None. Assert primary progress survives while readiness remains unassessed.
        """
        self.extra_fault = 'unknown_evidence'
        _, _, _, _, saved = self.run_document_task('explanation', 'Explain the supplied fact.', 'Check its source.')
        self.assertEqual(saved['coverage']['criteria_progress'][0]['status'], 'model_assessed_satisfied')
        self.assertEqual(saved['coverage']['readiness_progress'], [])
        self.assertEqual(saved['coverage']['history'][-1]['readiness_error'], 'unknown_or_changed_evidence')

    def test_unnecessary_study_keeps_a_method_without_acquisition(self):
        """An already defined evidence method can skip the pilot and still supply checks.

        Args:
            self: Case whose study planner judges the original task sufficiently defined.
        Returns:
            None. Assert only planning calls and no pilot search, selection or assessment.
        """
        self.flow.needed = False
        _, _, _, _, saved = self.flow.run_case()
        self.assertEqual(saved['coverage_study']['coverage_outcome'], 'unnecessary')
        self.assertEqual(self.flow.schemas, ['ResearchPlan', 'CoverageStudyPlan'])
        self.assertEqual(len(saved['coverage_study']['readiness']['coverage_checks']), 1)

    def test_ambiguity_prevents_method_from_being_marked_established(self):
        """An unresolved measurement boundary remains partial despite a confident model label.

        Args:
            self: Case leaving the sales period unspecified in the returned profile.
        Returns:
            None. Assert structural validation retains the ambiguity and downgrades the method.
        """
        self.flow.readiness['unresolved_questions'] = ['Which sales period?']
        _, _, _, _, saved = self.run_document_task('comparison', 'Compare sales.', 'Use comparable figures.')
        self.assertEqual(saved['coverage_study']['coverage_outcome'], 'partial')

    def test_full_length_requirements_reach_assessment_without_overflow_or_truncation(self):
        """Schema-valid study fields must remain valid when projected into main assessment.

        Args:
            self: Case supplying the maximum six requirements and maximum field lengths.
        Returns:
            None. Assert the production graph preserves every requirement separately and
            can assess the method without overflowing the criterion description field.
        """
        description = 'D' * 1600
        self.evidence_requirements = [str(i) * 1600 for i in range(6)]
        code, _, err, _, saved = self.run_document_task('explanation', description, 'Check the full requirements.')
        self.assertEqual(code, 0, err)
        criterion = self.coverage_inputs[-1]['criteria'][-1]
        self.assertEqual(criterion['description'], description)
        self.assertEqual(criterion['evidence_required'], self.evidence_requirements)
        self.assertEqual(saved['coverage']['readiness_progress'][0]['status'], 'model_assessed_satisfied')

    def test_legacy_study_records_need_no_reconstruction(self):
        """Old studies remain readable without fabricating new checks or replaying calls.

        Args:
            self: Case loading a minimal legacy study without the new profile fields.
        Returns:
            None. Assert no generated criteria and unchanged original legacy data.
        """
        from projectclarity.coverage.readiness import readiness_criteria, readiness_lines
        from tests.research.research_test_fixtures import example_session
        session = example_session(with_plan=True)
        session.coverage_study = {'status': 'inconclusive', 'estimates': []}
        self.assertEqual(readiness_criteria(session), [])
        self.assertEqual(readiness_lines(session.coverage_study, {}), [])
        self.assertEqual(session.coverage_study, {'status': 'inconclusive', 'estimates': []})

    def test_material_only_pilot_does_not_search_even_with_permission(self):
        """Search permission is not a requirement to search for a document-scoped pilot.

        Args:
            self: Case whose study plan includes queries but scopes evidence to local material.
        Returns:
            None. Assert no pilot query executes and the documented source alone is used.
        """
        _, _, _, _, saved = self.run_document_task('explanation', 'Explain the text.', 'Check its passages.', '--search')
        self.assertTrue(saved['request']['web_search'])
        self.assertEqual(saved['coverage_study']['query_ids'], [])
        self.assertEqual(saved['coverage_study']['search_status'], 'not_required_for_provided_material')
        self.assertFalse(self.flow.searches)

    def test_inapplicable_numeric_estimate_is_rejected(self):
        """A contradictory numeric proposal cannot turn a qualitative task into enumeration.

        Args:
            self: Case returning a quoted numeric range despite declaring it inapplicable.
        Returns:
            None. Assert the raw proposal survives but no estimate or stopping target is accepted.
        """
        self.flow.no_estimates = False
        _, _, _, _, saved = self.run_document_task('timeline', 'Order the events.', 'Compare temporal evidence.')
        study = saved['coverage_study']
        self.assertEqual(study['estimates'], [])
        self.assertEqual(study['rejected_estimates'][0]['error_code'], 'numeric_estimate_not_applicable')
        self.assertTrue(study['proposal']['estimates'])

    def test_check_ids_cannot_collide_with_original_criteria(self):
        """Supplemental checks must remain distinguishable even when a plan uses their prefix.

        Args:
            self: Case assigning CS1 to the original research criterion before the study.
        Returns:
            None. Assert the study assigns a different ID and leaves the plan untouched.
        """
        self.flow.cli.plan['completion_criteria'][0]['criterion_id'] = 'CS1'
        _, _, _, _, saved = self.flow.run_case()
        self.assertEqual(saved['coverage_study']['readiness']['coverage_checks'][0]['check_id'], 'S_CS1')
        self.assertEqual(saved['plan']['completion_criteria'][0]['criterion_id'], 'CS1')
