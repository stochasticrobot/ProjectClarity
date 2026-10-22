"""Verify the evaluator catches misses and extras without mistaking fixture scores for quality.

Handwritten sessions let us independently control correct/wrong chambers, aliases,
review status, quote integrity and duplicates. The scorer must not simply count
name mentions or turn acquisition failure into a successful empty result.
"""
import unittest
from evaluation.membership_reference_cases import load_suite, get_case
from evaluation.membership_result_scoring import score_session, reference_relations


class MembershipScoringTests(unittest.TestCase):
    """Use small synthetic outputs against the prepared, dated reference cases."""

    def setUp(self):
        """Create an empty case-matched session and a verified synthetic evidence slice."""
        self.suite = load_suite()
        self.case = get_case(self.suite, 'us_vermont_senate')
        self.text = 'A synthetic statement supplied only for scorer plumbing.'
        self.session = {'request': {'question': self.case['question']}, 'session_id': 'fixture',
            'sources': [{'source_id': f'S{i}', 'kind': 'url', 'url': s['url']} for i, s in enumerate(self.case['sources'], 1)],
            'status': 'candidates_saved', 'usage': {'requests_attempted': 3},
            'extraction': {'attempts': [], 'candidates': []}, 'claim_reviews': {},
            'documents': [{'source_id': 'S1', 'status': 'retrieved'}, {'source_id': 'S2', 'status': 'retrieved'}]}

    def add_relation(self, subject, predicate, obj, *, supported=True):
        """Add one independently controlled relationship and its synthetic evidence/review records."""
        index = len(self.session['extraction']['candidates']) + 1
        cid, bid = f'C{index}', f'B{index}'
        self.session['extraction']['attempts'].append({'batch_id': bid, 'status': 'completed', 'result': {
            'entities': [{'entity_id': 'E1', 'name': subject}, {'entity_id': 'E2', 'name': obj}]}})
        self.session['extraction']['candidates'].append({'candidate_id': cid, 'batch_id': bid, 'source_id': 'S1',
            'text_sha256': 'fixturehash', 'evidence_status': 'anchored', 'adjudication_status': 'supported' if supported else 'needs_review',
            'proposal': {'relationship': {'subject_id': 'E1', 'predicate': predicate, 'object_id': 'E2'}},
            'evidence': [{'source_id': 'S1', 'text_sha256': 'fixturehash', 'start': 0, 'end': len(self.text), 'quote': self.text}]})
        self.session['claim_reviews'][cid] = {'text_sha256': 'fixturehash', 'error_code': None,
            'assessment': {'verdict': 'supported', 'in_scope': supported}}

    def score(self):
        """Call the actual scorer without a model, network request or mock metric implementation."""
        return score_session(self.suite, self.case, self.session, {'S1': ('fixturehash', self.text)})

    def test_correct_aliases_and_duplicate_claims_count_once(self):
        """Bernard/Bernie is an explicit alias; repeated supporting sources cannot inflate recall."""
        self.add_relation('Bernard Sanders', 'MEMBER_OF', 'U.S. Senate')
        self.add_relation('Bernie Sanders', 'MEMBER_OF', 'United States Senate')
        self.add_relation('Bernie Sanders', 'REPRESENTS', 'Vermont')
        self.add_relation('Peter Welch', 'MEMBER_OF', 'United States Senate')
        self.add_relation('Peter Welch', 'REPRESENTS', 'Vermont')
        result = self.score()['supported_relationships']
        self.assertEqual((result['predicted_count'], result['correct_count'], result['recall']), (4, 4, 1.0))

    def test_wrong_chamber_and_former_member_are_false_positives(self):
        """Correct-looking names are insufficient: the person/chamber pair must match the reference."""
        self.add_relation('Peter Welch', 'MEMBER_OF', 'United States House of Representatives')
        self.add_relation('Patrick Leahy', 'MEMBER_OF', 'United States Senate')
        result = self.score()['supported_relationships']
        self.assertEqual(result['correct_count'], 0)
        self.assertEqual(len(result['unexpected']), 2)
        self.assertEqual(result['recall'], 0)

    def test_unreviewed_claims_and_unrelated_predicates_do_not_score_as_supported(self):
        """Extraction coverage and reviewed coverage are different observations."""
        self.add_relation('Peter Welch', 'MEMBER_OF', 'United States Senate', supported=False)
        self.add_relation('Bernie Sanders', 'KNOWS', 'Peter Welch')
        result = self.score()
        self.assertEqual(result['anchored_relationships']['correct_count'], 1)
        self.assertEqual(result['supported_relationships']['correct_count'], 0)
        self.assertEqual(result['ignored_candidate_counts']['not_a_requested_relationship'], 1)

    def test_invalid_offsets_and_absent_sources_cannot_score(self):
        """A correct reference triple without intact supporting evidence is not eligible."""
        self.add_relation('Peter Welch', 'MEMBER_OF', 'United States Senate')
        self.session['extraction']['candidates'][0]['evidence'][0]['end'] -= 1
        self.assertEqual(self.score()['anchored_relationships']['correct_count'], 0)
        self.session['documents'][1]['status'] = 'failed'
        self.session['documents'][1]['error_code'] = 'robots_unavailable'
        result = self.score()
        self.assertFalse(result['source_coverage_complete'])
        self.assertEqual(result['sources'][1]['error_code'], 'robots_unavailable')

    def test_empty_output_and_wrong_case_are_not_successful_research(self):
        """Empty predictions have zero recall; an unrelated question cannot be scored against this case."""
        result = self.score()['supported_relationships']
        self.assertEqual(result['recall'], 0)
        self.assertIsNone(result['precision'])
        self.session['request']['question'] = 'A different research objective'
        with self.assertRaisesRegex(ValueError, 'different research question'):
            self.score()

    def test_reference_pack_has_dates_sources_and_no_duplicate_expected_facts(self):
        """Every expected person has a dated source audit, without inventing human approval.

        This checks completeness of the recorded audit, not live political truth;
        the independent source inspection is documented in reference_validation.md.
        """
        self.assertEqual(self.suite['human_reference_review'], 'not_performed')
        validation = self.score()['reference_validation']
        self.assertEqual(validation['status'], 'source_verified')
        self.assertEqual(validation['reviewer'], 'assistant')
        self.assertEqual(validation['verified_on'], self.suite['reference_date'])
        self.assertEqual(validation['expected_members_checked'], 5)
        self.assertEqual(validation['expected_relationships_checked'], 10)
        self.assertEqual(len(self.suite['cases']), 4)
        checked = {(m['case_id'], m['name']): m for m in validation['checked_members']}
        self.assertEqual(len(checked), 5)
        for case in self.suite['cases']:
            with self.subTest(case=case['case_id']):
                self.assertEqual({s['kind'] for s in case['sources']}, {'official', 'wikipedia'})
                self.assertIn(self.suite['reference_date'], case['question'])
                relations, _ = reference_relations(self.suite, case)
                self.assertEqual(len(relations), 2 * len(case['expected_members']))
                if case.get('reference_case_ids'):
                    # The combined scenario reuses the audited facts, not a new
                    # unreviewed answer key or a second independent benchmark.
                    inherited = [m for case_id in case['reference_case_ids']
                                 for m in get_case(self.suite, case_id)['expected_members']]
                    self.assertEqual(case['expected_members'], inherited)
                    continue
                for member in case['expected_members']:
                    audit = checked[(case['case_id'], member['name'])]
                    for field in ['chamber', 'region', 'official_id', 'aliases']:
                        self.assertEqual(audit[field], member[field])
                    self.assertTrue(audit['official_sources'])
                    self.assertTrue(audit['cross_check_sources'])
                exclusion = next(e for e in validation['excluded_members_checked']
                                 if e['case_id'] == case['case_id'])
                self.assertEqual(exclusion['names'], case['negative_controls'])

    def test_old_frozen_reference_is_not_silently_marked_verified(self):
        """An earlier experiment keeps its original pending label, not today's new audit."""
        self.suite.pop('reference_validation')
        self.suite['human_reference_review'] = 'pending'
        result = self.score()
        self.assertEqual(result['human_reference_review'], 'pending')
        self.assertEqual(result['reference_validation']['status'], 'not_recorded')
