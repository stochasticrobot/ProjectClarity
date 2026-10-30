"""Check which proposed result records appear in the dashboard target counts.

Handwritten run data exercises grouping, pending/conflicting values, source-integrity
filters and estimate selection. A named entity is not automatically a requested
answer item. The file also checks coverage scheduling and response capacity with
controlled inputs; it does not assess a live model's judgment.
"""
import copy
import unittest
from types import SimpleNamespace

from projectclarity.dashboard.targets import target_groups, scope_summary
from projectclarity.research_results import result_progress
from projectclarity.coverage.checkpoints import coverage_checkpoint
from projectclarity.coverage.coverage_contracts import CriterionAssessment


class TargetTests(unittest.TestCase):
    """Keep targets, qualifications, incomplete records and counting units separate."""

    def data(self):
        """Create a small run-local observation unrelated to parliamentary benchmarks.

        Args:
            self: Test owner; no external resources are required.
        Returns:
            Independent dictionaries with a supported named item and attribution fields.
        """
        return dict(plan=dict(goals=[dict(goal_id='G1', question='List contributors.',
            result_shape=dict(unit='one contributor', required_fields=['name', 'affiliation']))]),
            extraction=dict(candidates=[dict(candidate_id='C1', evidence_status='anchored',
                adjudication_status='supported', proposal=dict(goal_ids=['G1'],
                    research_roles=['answer_evidence'], claim_kind='non_relational',
                    qualifications=['Affiliation at publication time only.'], entity_ids=[],
                    result_records=[dict(goal_id='G1', fields=[dict(name='name', value='Aster'),
                        dict(name='affiliation', value='North')])]))]))

    def test_different_result_context_is_not_collapsed_by_name(self):
        """Matching names with different required fields must not silently merge.

        Args:
            self: Fixture with one name, multiple field variants and an unresolved alias.
        Returns:
            None. Assert all field-distinct results survive, with qualifications and no mutation.
        """
        data = self.data()
        original = data['extraction']['candidates'][0]
        for i, (name, place) in enumerate([(' ASTER ', 'South'), ('A. Aster', 'North')], 2):
            item = copy.deepcopy(original)
            item['candidate_id'] = f'C{i}'
            item['proposal']['result_records'][0]['fields'] = [dict(name='name', value=name),
                dict(name='affiliation', value=place)]
            data['extraction']['candidates'].append(item)
        before = copy.deepcopy(data)
        goal = target_groups(result_progress(data))[0]
        self.assertEqual((goal['identified'], goal['supported']), (3, 3))
        self.assertEqual(goal['count_basis'], 'goal-results-v2')
        aster = next(r for r in goal['rows'] if r['label'] == 'Aster')
        self.assertEqual(aster['variants'], 1)
        self.assertEqual(aster['candidate_ids'], ['C1'])
        self.assertTrue(aster['qualifications'])
        self.assertEqual(data, before)

    def test_leads_conflicts_incomplete_and_integrity_filters(self):
        """A source-supported lead or partial tuple must not be promoted to a supported target.

        Args:
            self: Fixture with altered roles, verdicts and required fields.
        Returns:
            None. Assert pending leads, disputes, incomplete exclusion and invalidated evidence.
        """
        data = self.data()
        claim = data['extraction']['candidates'][0]
        claim['proposal']['research_roles'] = ['lead']
        goal = target_groups(result_progress(data))[0]
        self.assertEqual((goal['identified'], goal['supported'], goal['pending']), (1, 0, 1))
        claim['adjudication_status'] = 'contradicted'
        self.assertEqual(target_groups(result_progress(data))[0]['disputed'], 1)
        claim['proposal']['result_records'][0]['fields'].pop()
        goal = target_groups(result_progress(data))[0]
        self.assertEqual((goal['identified'], goal['incomplete']), (0, 1))
        self.assertEqual(target_groups(result_progress(self.data(), set()))[0]['identified'], 0)

    def test_non_person_shapes_and_goals_remain_separate(self):
        """Unknown units use their full fields; identical labels across goals do not merge.

        Args:
            self: Fixture with people and independently enumerated catalogue items.
        Returns:
            None. Assert no name-field guess for arbitrary shapes and no cross-goal total.
        """
        data = self.data()
        goal = copy.deepcopy(data['plan']['goals'][0])
        goal.update(goal_id='G2', result_shape=dict(unit='one artifact', required_fields=['item', 'place']))
        data['plan']['goals'].append(goal)
        item = copy.deepcopy(data['extraction']['candidates'][0])
        item['candidate_id'] = 'C2'
        item['proposal']['goal_ids'] = ['G2']
        item['proposal']['result_records'] = [dict(goal_id='G2', fields=[
            dict(name='item', value='Aster'), dict(name='place', value='North')])]
        data['extraction']['candidates'].append(item)
        groups = target_groups(result_progress(data))
        self.assertEqual([g['identified'] for g in groups], [1, 1])
        self.assertIsNone(groups[1]['name_field'])
        self.assertNotEqual(groups[0]['rows'][0]['id'], groups[1]['rows'][0]['id'])

    def test_study_estimate_supersedes_initial_copy_until_a_real_revision(self):
        """Coverage initialization copies the plan; it must not hide the later study estimate.

        Args:
            self: Fixture with ranges and explicit provenance, without researched denominators.
        Returns:
            None. Assert correct per-goal estimate precedence without mutation.
        """
        data = self.data()
        initial = dict(goal_ids=['G1'], lower=10, upper=40, basis='provisional_prior')
        study = dict(goal_ids=['G1'], range=dict(lower=20, upper=30), basis='evidence_informed')
        data['plan']['scope_estimates'] = [initial]
        data['coverage'] = dict(scope_estimates=[initial], history=[])
        data['coverage_study'] = dict(estimates=[study])
        self.assertEqual(scope_summary(data)[0]['range'], study['range'])
        data['coverage'].update(scope_estimates=[dict(initial, lower=22)], history=[dict(status='assessed')])
        self.assertEqual(scope_summary(data)[0]['lower'], 22)

    def test_coverage_checkpoint_is_independent_bounded_and_review_first(self):
        """Coverage checks do not depend on stopping and cannot run every page.

        Args:
            self: Saved-task reservations and already budget-checked task inventory.
        Returns:
            None. Assert coverage enablement, twelve-call spacing and restart persistence.
        """
        session = SimpleNamespace(request=SimpleNamespace(stop_when_satisfied=False,
            assess_coverage=False, extract_knowledge=True, model_retries=2,
            budget=SimpleNamespace(max_requests=100)), plan=SimpleNamespace(completion_criteria=[True]),
            usage=SimpleNamespace(requests_attempted=12), workflow=dict(tasks={}))
        tasks = [dict(action='extract_knowledge'), dict(action='assess_research_gaps')]
        self.assertIsNone(coverage_checkpoint(session, tasks))
        session.request.assess_coverage = True
        self.assertEqual(coverage_checkpoint(session, tasks), [tasks[1]])
        review = dict(action='review_claims')
        self.assertEqual(coverage_checkpoint(session, tasks + [review]), [review])
        session.workflow['tasks']['coverage'] = dict(action='assess_research_gaps',
            attempts=1, status='uncertain', before_model_requests=12)
        session.usage.requests_attempted = 23
        self.assertIsNone(coverage_checkpoint(session, tasks))
        session.usage.requests_attempted = 24
        self.assertEqual(coverage_checkpoint(session, tasks), [tasks[1]])

    def test_coverage_contract_can_represent_a_roster_larger_than_100(self):
        """A numerical assessment must be capable of returning a full parliamentary-size list.

        Args:
            self: Pure schema test; values are invented, not live-model verification.
        Returns:
            None. Assert 343 separately named items survive parsing without truncation.
        """
        row = CriterionAssessment(criterion_id='K1', judgment='satisfied', target_count=343,
            items=[dict(key=f'Item {i}', candidate_ids=[f'C{i}']) for i in range(343)],
            benchmark_candidate_ids=['B1'], evidence_candidate_ids=[], gaps=[], rationale='Synthetic roster.')
        self.assertEqual(len(row.items), 343)
