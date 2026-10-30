"""Check that claim support, complete result fields and item identity remain distinct.

Handwritten observations exercise repeated evidence, competing values, missing or
invalid fields, changed sources and goals without list-shaped answers. Direct
helper checks also cover one-level repair attempts and coverage freshness. No
model generates these inputs; a source-supported sentence alone must not create
a fully supported report entry.
"""
import copy
import unittest

from projectclarity.coverage.freshness import coverage_freshness
from projectclarity.coverage.stopping import evidence_revision
from projectclarity.research_results import result_progress, record_check, result_defects
from tests.extraction import test_record_refinement as refinement_tests
from tests.research.research_test_fixtures import example_session
from projectclarity.extraction.record_refinement import record_followups


class ResultAccountingTests(unittest.TestCase):
    """Use generic inventory items instead of parliamentary benchmark answers."""

    def setUp(self):
        """Build a shaped goal and fully supported source-scoped observation.

        Args:
            self: Test fixture owning independent mutable dictionaries.
        Returns:
            None. No files, networks or model requests are involved.
        """
        self.goal = {'goal_id': 'G1', 'question': 'List items and locations.',
            'result_shape': {'unit': 'items', 'required_fields': ['item', 'location']}}
        self.candidate = {'candidate_id': 'S1_P1_C1', 'evidence_status': 'anchored',
            'adjudication_status': 'supported', 'proposal': {'goal_ids': ['G1'],
                'statement': 'Aster is in North.', 'qualifications': [], 'research_roles': ['answer_evidence'],
                'claim_kind': 'relational', 'entity_ids': ['A', 'B'],
                'relationship': {'subject_id': 'A', 'predicate': 'in', 'object_id': 'B'},
                'result_records': [{'goal_id': 'G1', 'fields': [
                    {'name': 'item', 'value': 'Aster'}, {'name': 'location', 'value': 'North'}]}]}}

    def progress(self, candidates, **kwargs):
        """Compute the public report projection from only supplied run-local observations.

        Args:
            self: Fixture defining a result shape.
            candidates: Independent source observations to classify and group.
            kwargs: Optional integrity-filter arguments forwarded to result_progress.
        Returns:
            The goal's result inventory and factual counts, without mutation.
        """
        return result_progress({'session_id': 'one-run', 'plan': {'goals': [self.goal]},
            'extraction': {'candidates': candidates}}, **kwargs)['goals'][0]

    def test_duplicates_merge_observations_but_not_conflicting_values_or_aliases(self):
        """Do not inflate duplicates or invent cross-source identity resolution.

        Args:
            self: Complete observation, exact duplicate, alias and different location.
        Returns:
            None; three distinct tuples remain, and the duplicate retains both citations.
        """
        items = [copy.deepcopy(self.candidate) for _ in range(4)]
        for i, item in enumerate(items):
            item['candidate_id'] = f'S{i}_P1_C1'
        items[1]['proposal']['result_records'][0]['fields'][0]['value'] = ' Aster  '
        items[2]['proposal']['result_records'][0]['fields'][0]['value'] = 'A. Aster'
        items[3]['proposal']['result_records'][0]['fields'][1]['value'] = 'South'
        result = self.progress(items)['results']
        self.assertEqual(result['supported'], 3)
        self.assertEqual(len(result['records'][0]['candidate_ids']), 2)

    def test_conflicting_verdict_prevents_the_same_tuple_being_reported_supported(self):
        """Expose disagreement rather than accepting whichever source appears first.

        Args:
            self: Two observations with identical fields and conflicting reviews.
        Returns:
            None; the tuple is disputed and both candidate references survive.
        """
        other = copy.deepcopy(self.candidate)
        other.update(candidate_id='S2_P1_C1', adjudication_status='contradicted')
        for candidates in ([self.candidate, other], [other, self.candidate]):
            result = self.progress(candidates)['results']
            self.assertEqual((result['supported'], result['disputed']), (0, 1))
            self.assertEqual(len(result['records'][0]['candidate_ids']), 2)

    def test_truth_support_does_not_fill_missing_structure_and_fragments_are_not_joined(self):
        """Retain true prose while refusing to invent a relationship or combine fragments.

        Args:
            self: Complete fields with no edge, then complementary partial observations.
        Returns:
            None; structural incompleteness cannot produce a supported complete record.
        """
        self.candidate['proposal']['relationship'] = None
        result = self.progress([self.candidate])
        self.assertEqual(result['supported_facts'], 1)
        self.assertEqual(result['needs_structure'], 1)
        self.assertEqual((result['results']['supported'], result['results']['pending']), (0, 1))
        other = copy.deepcopy(self.candidate)
        other['candidate_id'] = 'S2_P1_C1'
        self.candidate['proposal']['result_records'][0]['fields'].pop()
        other['proposal']['result_records'][0]['fields'].pop(0)
        result = self.progress([self.candidate, other])['results']
        self.assertEqual((result['supported'], result['incomplete']), (0, 2))

    def test_invalid_out_of_scope_and_changed_sources_do_not_count(self):
        """Exclude non-answer observations even if a model called the prose supported.

        Args:
            self: Complete record modified to exercise each exclusion boundary.
        Returns:
            None; none of the excluded observations contributes a supported item.
        """
        cases = [copy.deepcopy(self.candidate) for _ in range(2)]
        cases[0]['evidence_status'] = 'invalid'
        cases[1]['adjudication_status'] = 'out_of_scope'
        for candidate in cases:
            self.assertEqual(self.progress([candidate])['results']['supported'], 0)
        self.assertEqual(self.progress([self.candidate], valid_ids=set())['results']['supported'], 0)

    def test_missing_unknown_and_duplicate_fields_remain_distinguishable(self):
        """Keep complete-looking malformed records from quietly entering target counts.

        Args:
            self: A valid tuple varied with extra, duplicated or omitted keys.
        Returns:
            None; invalid and incomplete records are visible and never counted complete.
        """
        record = self.candidate['proposal']['result_records'][0]
        for field in ({'name': 'invented', 'value': 'x'}, {'name': 'item', 'value': 'different'}):
            altered = copy.deepcopy(record)
            altered['fields'].append(field)
            self.assertEqual(record_check(altered, self.goal)['status'], 'invalid')
        record['fields'].pop()
        self.assertEqual(record_check(record, self.goal)['missing_fields'], ['location'])
        self.assertEqual(self.progress([self.candidate])['results']['incomplete'], 1)

    def test_empty_or_equal_partial_records_do_not_imply_the_same_target(self):
        """Missing identity fields must not collapse unrelated incomplete observations.

        Args:
            self: Two source-scoped claims with equal partial or entirely absent values.
        Returns:
            None; both observations remain visible without inventing two distinct targets.
        """
        other = copy.deepcopy(self.candidate)
        other['candidate_id'] = 'S2_P1_C1'
        for candidate in (self.candidate, other):
            candidate['proposal']['result_records'][0]['fields'].pop()
        self.assertEqual(self.progress([self.candidate, other])['results']['incomplete'], 2)
        for candidate in (self.candidate, other):
            candidate['proposal']['result_records'][0]['fields'] = []
        result = self.progress([self.candidate, other])['results']
        self.assertEqual(result['incomplete'], 2)
        self.assertEqual(result['supported'], 0)

    def test_non_enumeration_goal_does_not_get_a_fabricated_target_count(self):
        """Explain an unshaped goal with facts instead of guessing what is being counted.

        Args:
            self: A legacy/non-enumeration goal with otherwise valid evidence.
        Returns:
            None; no result inventory is fabricated from entities or claim counts.
        """
        self.goal['result_shape'] = None
        row = self.progress([self.candidate])
        self.assertIsNone(row['results'])
        self.assertEqual(row['supported_facts'], 1)

    def test_answer_claim_without_an_item_record_is_retained_not_forced_into_one(self):
        """Honor the prompt's empty-list allowance for claims that do not identify an item.

        Args:
            self: Shaped goal with a supported non-relational population-total claim.
        Returns:
            None; the fact is visible, no target is invented and no record retry is requested.
        """
        claim = self.candidate['proposal']
        claim.update(statement='The inventory contains 12 items.', claim_kind='non_relational',
            relationship=None, entity_ids=[], result_records=[])
        self.assertEqual(result_defects(claim, [self.goal]), [])
        row = self.progress([self.candidate])
        self.assertEqual(row['supported_facts'], 1)
        self.assertEqual(row['results']['claims_without_records'], 1)
        self.assertEqual(row['results']['supported'], 0)

    def test_changed_review_at_equal_candidate_count_invalidates_coverage(self):
        """Coverage freshness compares evidence, not just elapsed time or record totals.

        Args:
            self: Typed fixture with a saved current evidence fingerprint.
        Returns:
            None; a changed verdict invalidates the assessment without changing totals.
        """
        session = example_session()
        session.coverage = {'assessment_status': 'assessed', 'history': [
            {'candidate_total': 0, 'evidence_revision': evidence_revision(session)}]}
        self.assertEqual(coverage_freshness(session)['status'], 'fresh')
        session.claim_reviews['changed'] = {'verdict': 'contradicted'}
        self.assertEqual(coverage_freshness(session)['status'], 'stale')


class ExplicitRefinementTests(unittest.TestCase):
    """Check one-record repairs and restart reconciliation without model calls."""

    def test_explicit_single_record_defect_is_bounded_and_restart_stable(self):
        """A declared missing relationship gets one child without rewriting the original.

        Args:
            self: Assertions for deterministic scheduling and child non-recursion.
        Returns:
            None; original proposals survive and re-inventory yields the same child ID.
        """
        fixture = refinement_tests.RecordRefinementTests()
        text, parent = fixture.attempt()
        parent.end = text.index('[Record T1R3]')
        parent.result.claims[0].claim_kind = 'relational'
        before = parent.result.model_dump()
        children = record_followups(parent, text)
        self.assertEqual(len(children), 1)
        self.assertEqual(parent.result.model_dump(), before)
        self.assertEqual(children[0].batch_id, record_followups(parent, text)[0].batch_id)
        child = children[0]
        child.status, child.result = 'completed', parent.result
        self.assertEqual(record_followups(child, text), [])

    def test_declared_nonrelational_or_unanchorable_prose_does_not_trigger_edge_repair(self):
        """Do not fabricate work for legitimate non-relational facts or invalid quotations.

        Args:
            self: Synthetic completed record receipt from the legacy test fixture.
        Returns:
            None; non-relational claims and missing evidence do not trigger an edge repair.
        """
        text, parent = refinement_tests.RecordRefinementTests().attempt()
        parent.result.claims[0].claim_kind = 'non_relational'
        self.assertEqual(record_followups(parent, text), [])
        parent.result.claims[0].claim_kind = 'relational'
        parent.end = text.index('[Record T1R3]')
        parent.result.claims[0].evidence[0].quote = 'Not present in the passage.'
        self.assertEqual(record_followups(parent, text), [])


class LeadRecordVisibilityTests(unittest.TestCase):
    """Keep explicitly proposed target records visible without approving leads."""

    def test_supported_lead_record_is_pending_not_dropped_or_promoted(self):
        """A source-supported lead can still identify an unresolved target item.

        Args:
            self: Assertion owner using the generic item fixture without a model.
        Returns:
            None; checks retained fields/provenance and no supported-target inflation.
        """
        fixture = ResultAccountingTests()
        fixture.setUp()
        fixture.candidate['proposal']['research_roles'] = ['lead']
        result = fixture.progress([fixture.candidate])
        self.assertEqual((result['results']['supported'], result['results']['pending']), (0, 1))
        self.assertEqual(result['supported_facts'], 0)
        self.assertEqual(result['results']['records'][0]['values'], {'item': 'Aster', 'location': 'North'})
        fixture.candidate['adjudication_status'] = 'out_of_scope'
        self.assertEqual(fixture.progress([fixture.candidate])['results']['pending'], 0)
