"""Exercise coverage contracts and diagnostics without model-scored truth claims.

Portable synthetic cases retain date/identity/conflict evidence, while authored
answers probe production validation. Full CLI persistence is tested separately
in test_contract_completion and scheduler behavior in test_answer_controller_cli.
"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluation.coverage_case_validation import validate_case
from projectclarity.coverage.assessment_diagnostics import begin_assessment, finish_assessment
from projectclarity.coverage.contract_assessment import queue_searches
from projectclarity.coverage.coverage_contracts import CoverageAssessment
from projectclarity.coverage.payload_normalization import normalize_qualification_payload
from projectclarity.nodes.assess_research_gaps.node import validate_assessment
from projectclarity.research_schemas import CompletionCriterion
from tests.research.research_test_fixtures import example_session


def case(name):
    """Read a portable fixture containing source data, never a copied system prompt.

    Args:
        name: A for historical dates or B for distinct identities and party conflict.
    Returns:
        Fresh coverage JSON; no credentials or private research fixtures are accessed.
    """
    return json.loads((Path(__file__).parent / 'fixtures/coverage' / (name + '.json')).read_text())


def reply(searches=()):
    """Construct a minimal schema-valid response for queue and failure tests.

    Args:
        searches: Authored query strings with an existing goal; never executed.
    Returns:
        CoverageAssessment with no criteria, suitable only for focused queue tests.
    """
    return CoverageAssessment(assessments=[], next_searches=[dict(query=q, goal_ids=['G1'],
        reason='Offline queue test.') for q in searches])


class CoverageDiagnosticsTests(unittest.TestCase):
    """Keep fixture validity, model proposals and application acceptance distinct."""

    def test_fixture_preflight_rejects_the_observed_contract_and_attribution_faults(self):
        """Catch the original live-pack mistakes before another provider request.

        Args:
            self: Unittest owner with portable copies of corrected source data.
        Returns:
            None. Both valid arms pass; independently injected faults raise ValueError.
        """
        for name in ('A', 'B'):
            payload = case(name)
            self.assertEqual(validate_case(payload), payload)
            normalized = normalize_qualification_payload(payload)
            self.assertNotEqual(normalized, payload)
            self.assertEqual(validate_case(normalized), payload)
            for fault in ('kind', 'source', 'unit', 'candidate'):
                broken = deepcopy(payload)
                if fault == 'kind':
                    broken['criteria'][1]['kind'] = 'supporting'
                elif fault == 'source':
                    broken['candidates'][1]['source_id'] = 'S1'
                elif fault == 'unit':
                    broken['criteria'][0]['unit'] = 'seat capacity including vacancies'
                else:
                    broken['result_inventory']['G1']['items'][0]['candidate_ids'] = ['invented']
                with self.subTest(name=name, fault=fault), self.assertRaises(ValueError):
                    validate_case(broken)

    def test_shared_data_preserves_historical_dates_and_distinct_conflicting_people(self):
        """Inspect specific decisive facts after reference expansion, not just byte size.

        Args:
            self: Unittest owner; no semantic model response is simulated here.
        Returns:
            None. Independent date/name/conflict assertions survive the real encoder.
        """
        a = validate_case(normalize_qualification_payload(case('A')))
        historical = a['result_inventory']['G1']['items'][1]
        context = historical['qualification_requirements'][0]['assessments'][0]['context']
        self.assertIn('2024-01-01', context['source_stated_metadata']['declarations'][0])
        self.assertIn('2026-10-30', context['acquired_at'])
        b = validate_case(normalize_qualification_payload(case('B')))
        rows = b['result_inventory']['G1']['items']
        self.assertEqual([r['fields']['member_name'] for r in rows], ['John K Smith', 'John Kenneth Smith'])
        declaration = rows[0]['qualification_requirements'][0]['assessments'][0]['context']['source_stated_metadata']['declarations']
        self.assertIn('B101', declaration[0])
        self.assertIn('B202', declaration[0])
        self.assertIn('Blue', declaration[1])
        self.assertIn('Red', declaration[1])
        self.assertIn('Neither document supersedes', declaration[1])

    def test_unknown_targets_and_satisfied_with_gaps_remain_blocked(self):
        """Run authored adverse responses through the same validator as production.

        Args:
            self: Test owner supplying minimal offered-reference objects, not fake source verification.
        Returns:
            None. Positive supporting evidence can pass independently, but unknown
            enumeration targets and satisfied-with-gaps proposals cannot become complete.
        """
        for name in ('A', 'B'):
            payload = validate_case(case(name))
            session = example_session()
            session.plan.completion_criteria = [CompletionCriterion.model_validate({k:v for k,v in row.items()
                if k in CompletionCriterion.model_fields}) for row in payload['criteria']]
            offered = {row['candidate_id']: SimpleNamespace(adjudication_status='supported',
                proposal=SimpleNamespace(goal_ids=['G1'])) for row in payload['candidates']}
            answer = CoverageAssessment(assessments=[dict(criterion_id=c['criterion_id'],
                judgment='satisfied', items=[], target_count=None, result_goal_id=c['result_goal_id'],
                benchmark_candidate_ids=[], evidence_candidate_ids=list(offered), gaps=[],
                rationale='Authored validator test, not a model judgment.') for c in payload['criteria']], next_searches=[])
            checked = validate_assessment(answer, session, offered, result_ledgers=payload['result_inventory'])
            self.assertEqual(checked[0]['status'], 'partial')
            self.assertEqual(checked[1]['status'], 'model_assessed_satisfied')
            answer.assessments[1].gaps = ['Unresolved source attribution.']
            checked = validate_assessment(answer, session, offered, result_ledgers=payload['result_inventory'])
            diagnostic = begin_assessment(payload, {})
            finish_assessment(diagnostic, answer, checked)
            self.assertEqual(checked[1]['status'], 'partial')
            self.assertIn('unresolved_gaps', diagnostic['criteria'][1]['blocking_reasons'])
            self.assertIn('satisfied_proposal_has_gaps', diagnostic['warnings'])
            answer.assessments[1].evidence_candidate_ids = ['invented']
            with self.assertRaisesRegex(ValueError, 'unknown_or_changed_evidence'):
                validate_assessment(answer, session, offered, result_ledgers=payload['result_inventory'])

    def test_search_dispositions_do_not_invent_semantic_duplicates_or_execute_work(self):
        """Differentiate exact repeats from novel wording, preserving queue policy.

        Args:
            self: Test owner with a backlog and an already attempted exact query.
        Returns:
            None. Existing deduplication holds, warning is factual and model budget stays zero.
        """
        session = example_session()
        session.request.web_search = True
        session.search = {'queries':[{'query':'Original roster'}]}
        session.coverage = {'next_searches':[]}
        payload = {'acquired_work':[dict(source_id='S1', unread_passages=7, pending_review=2)]}
        diagnostic = begin_assessment(payload, {})
        answer = reply([' ORIGINAL  roster ', 'New roster', 'new roster', 'Original member list'])
        self.assertTrue(queue_searches(session, answer, diagnostic))
        finish_assessment(diagnostic, answer, [])
        self.assertEqual([d['disposition'] for d in diagnostic['search_decisions']],
                         ['already_attempted','queued_not_executed','duplicate_proposal','queued_not_executed'])
        self.assertEqual(len(session.coverage['next_searches']), 2)
        self.assertIn('search_proposed_with_local_backlog', diagnostic['warnings'])
        self.assertEqual(session.usage.requests_attempted, 0)
        self.assertNotIn('query', diagnostic['search_decisions'][0])
        payload['acquired_work'][0]['unread_passages'] = 0
        self.assertEqual(diagnostic['source_work'][0]['unread_passages'], 7)

    def test_disabled_and_invalid_searches_and_provider_failure_are_explicit(self):
        """Retain denied admissions and unknown usage without falsely accepting output.

        Args:
            self: Test owner probing failure branches with no credentials or network.
        Returns:
            None. Queue remains unchanged on invalid goals; missing output stays not_run.
        """
        session = example_session()
        session.coverage = {'next_searches':[]}
        answer = reply(['One query'])
        diagnostic = begin_assessment({}, {})
        self.assertTrue(queue_searches(session, answer, diagnostic))
        self.assertEqual(diagnostic['search_decisions'][0]['disposition'], 'search_disabled')
        answer.next_searches[0].goal_ids = ['UNKNOWN']
        self.assertFalse(queue_searches(session, answer, diagnostic))
        self.assertEqual(diagnostic['search_decisions'][0]['disposition'], 'rejected_unknown_goal_batch')
        self.assertEqual(session.coverage['next_searches'], [])
        finish_assessment(diagnostic, None, error='model_timeout')
        self.assertEqual(diagnostic['validation_status'], 'not_run')
        self.assertEqual(diagnostic['error_code'], 'model_timeout')

    def test_oversize_aggregate_records_final_projection_without_dispatch(self):
        """Keep the final aggregate measurements on a deliberately forced cap failure.

        Args:
            self: Test owner supplying genuinely oversized fixed context for this error-path probe.
        Returns:
            None. Real assessment preparation/persistence retains the aggregate hash,
            and never reads credentials or invokes a model. This is not a scale test.
        """
        from projectclarity.answer_schema.binding import bind_report
        from projectclarity.coverage import contract_assessment, bounded_assessment
        from projectclarity.extraction.extraction_contracts import ExtractionRecord
        from tests.research.test_contract_completion import explicit_specification
        session = example_session()
        session.answer_schema = explicit_specification()
        bind_report(session.plan, session.answer_schema)
        session.extraction = ExtractionRecord().model_dump(mode='json')
        session.review_decision = {'note': 'Oversized fixed context ' * 7000}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / session.session_id).mkdir()
            with patch.object(
                    bounded_assessment, 'invoke_structured', side_effect=AssertionError('No dispatch allowed')):
                contract_assessment.assess_contract({'session':session}, root,
                    key_reader=lambda: self.fail('Credentials must not be read'))
            row = json.loads((root / session.session_id / 'coverage.json').read_text())['history'][-1]
            self.assertEqual(row['error_code'], 'bounded_final_context_too_large')
            self.assertGreater(row['direct_input_characters'], 128_000)
            self.assertEqual(row['diagnostics']['validation_status'], 'not_run')
            self.assertEqual(session.usage.requests_attempted, 0)

    def test_population_support_is_independent_of_result_field_completeness(self):
        """Preserve explicit totals while exposing bad benchmarks independently.

        Args:
            self: Test owner running production validation on authored responses.
        Returns:
            None. Field conflicts do not erase a supported benchmark proposal;
            unreviewed, contradicted and wrong-goal benchmarks cannot establish it
            or become a report/dashboard percentage denominator.
            This does not score the semantic entailment of a live model's total.
        """
        payload = validate_case(case('B'))
        session = example_session()
        session.plan.completion_criteria = [CompletionCriterion.model_validate({k: v for k, v in row.items()
            if k in CompletionCriterion.model_fields}) for row in payload['criteria']]
        offered = {row['candidate_id']: SimpleNamespace(adjudication_status='supported',
            proposal=SimpleNamespace(goal_ids=['G1'])) for row in payload['candidates']}
        benchmark = next(iter(offered))
        answer = CoverageAssessment(assessments=[dict(criterion_id=c['criterion_id'],
            judgment='partial', items=[], target_count=2 if c['kind'] == 'enumeration' else None,
            result_goal_id=c['result_goal_id'], benchmark_candidate_ids=[benchmark] if c['kind'] == 'enumeration' else [],
            evidence_candidate_ids=list(offered), gaps=['Unresolved attribute conflict.'],
            rationale='The population is two even though a party field conflicts.') for c in payload['criteria']], next_searches=[])
        for status, goals, expected in [('supported', ['G1'], 'model_asserted_supported_benchmark'),
                ('pending', ['G1'], 'unverified'), ('contradicted', ['G1'], 'unverified'),
                ('supported', ['G2'], 'unverified')]:
            with self.subTest(status=status, goals=goals):
                offered[benchmark].adjudication_status = status
                offered[benchmark].proposal.goal_ids = goals
                checked = validate_assessment(answer, session, offered, result_ledgers=payload['result_inventory'])
                self.assertEqual(checked[0]['population']['state'], expected)
                self.assertEqual(checked[0]['target_count'], 2)
                self.assertEqual(checked[0]['status'], 'partial')
                self.assertEqual('population_benchmark_not_supported' in checked[0]['blocking_reasons'], expected == 'unverified')
                self.assertEqual(checked[1]['population']['state'], 'not_applicable')
                from projectclarity.goal_measurement import measure_goal
                data = session.model_dump(mode='json')
                data['coverage'] = {'criteria_progress': checked}
                data['extraction'] = {'candidates': [dict(candidate_id=cid, evidence_status='anchored',
                    adjudication_status=c.adjudication_status) for cid, c in offered.items()]}
                measurement = measure_goal(data, {'goal_id':'G1'}, payload['result_inventory']['G1'])
                self.assertEqual(measurement['target'], 2 if expected == 'model_asserted_supported_benchmark' else None)
                if expected == 'unverified':
                    self.assertIsNone(measurement['supported_percent'])
        answer.assessments[0].target_count = None
        checked = validate_assessment(answer, session, offered, result_ledgers=payload['result_inventory'])
        self.assertEqual(checked[0]['population']['state'], 'unknown')
        self.assertIsNone(checked[0]['population']['proposed_count'])

    def test_reference_translation_refuses_ambiguity_and_never_expands_benchmarks(self):
        """Limit citation repair to exact, wholly offered provenance mappings.

        Args:
            self: Test owner exercising adversarial identifiers without a model.
        Returns:
            None. Missing, ambiguous and benchmark IDs remain for rejection;
            direct candidate IDs win collisions and raw responses remain unchanged.
        """
        from projectclarity.coverage.reference_normalization import normalize_result_references
        answer = CoverageAssessment(assessments=[dict(criterion_id='C1', judgment='partial',
            items=[dict(key='One item', candidate_ids=['row'])], target_count=2,
            benchmark_candidate_ids=['row'], evidence_candidate_ids=['row', 'A', 'missing', 'ambiguous', 'incomplete'],
            gaps=[], rationale='Authored reference-boundary probe.')], next_searches=[])
        before = answer.model_dump()
        ledgers = {'G1': {'items': [dict(result_id=k, candidate_ids=v) for k, v in
            [('row', ['A','B']), ('A', ['B']), ('ambiguous', ['A']), ('ambiguous', ['B']),
             ('incomplete', ['A','unoffered'])]]}}
        normalized, receipt = normalize_result_references(answer, {'A':None, 'B':None}, ledgers)
        row = normalized.assessments[0]
        self.assertEqual(row.evidence_candidate_ids, ['A','B','missing','ambiguous','incomplete'])
        self.assertEqual(row.items[0].candidate_ids, ['A','B'])
        self.assertEqual(row.benchmark_candidate_ids, ['row'])
        self.assertEqual(receipt, {'C1': {'row':['A','B']}})
        self.assertEqual(answer.model_dump(), before)
