"""Exercise separate factual and inclusion judgments through production reporting.

The CLI cases substitute HTTP/search/model replies, not graph nodes, quotation
validation, report assembly or stopping. Authored judgments prove that the program
keeps facts and eligibility separate; they do not prove live model accuracy on
undated official listings or establish anyone's real-world current status.
"""
import copy
import json
import unittest

from projectclarity.claim_review.qualification import POLICY, requirement_catalog, record_fingerprint
from projectclarity.coverage.stopping import completion_status, evidence_revision
from projectclarity.coverage.result_inventory import coverage_results
from projectclarity.research_results import result_progress
from projectclarity.research_schemas import ResearchSession
from projectclarity.nodes.assess_research_gaps.node import checked_candidates
from projectclarity.answer_schema.render import render_report
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
from tests.research.adversarial_source_scenarios import SourceScenario
from tests.research.test_source_identity import source_fixture


class QualificationTests(unittest.TestCase):
    """Require source-bound inclusion evidence without rewriting factual verdicts."""

    def run_case(self, fault=None):
        """Run a small dated report through the public CLI with controlled review replies.

        Args:
            self: Test owner retaining isolated run files until assertions finish.
            fault: Optional missing, unknown, quote, index, duplicate, timestamp or
                not_met qualification fault. None supplies positive fixture judgments.
        Returns:
            Typed saved session, run directory and captured serialized review requests.
            The coverage model deliberately claims success even when qualification
            failed, so production completion checks must independently reject it.
        """
        scenario = SourceScenario(self, count=2, arrivals=0, concurrency=1)
        case = scenario.case
        case.schema['as_of'] = '2026-10-30'
        original = case.provider
        seen = []

        def provider(request):
            """Return independent factual and inclusion outcomes at the SDK boundary.

            Args:
                request: Actual serialized provider request from the production graph.
            Returns:
                Synthetic response; unknown tasks delegate to the source scenario.
                No stored candidate status or completion gate is patched by the test.
            """
            wire = json.loads(request.content)
            title = wire['output_config']['format']['schema']['title']
            payload = json.loads(wire['messages'][0]['content'])
            if title == 'ClaimReviewBatch':
                seen.append(wire)
                assessments = []
                kinds = {r['requirement_id']: r['kind'] for rows in payload['report_requirements'].values() for r in rows}
                for c in payload['candidates']:
                    decisions = fixture_eligibility(c, payload)
                    for decision in decisions:
                        if kinds[decision['requirement_id']] != 'currentness':
                            continue
                        if fault == 'unknown':
                            decision.update(verdict='unknown', rationale='No currentness evidence in this fixture.', evidence_quotes=[])
                        elif fault == 'quote':
                            decision['evidence_quotes'] = ['This text does not occur in the assigned source.']
                        elif fault == 'index':
                            decision['record_index'] = 7
                        elif fault == 'timestamp':
                            decision.update(evidence_quotes=[], context_keys=['acquired_at'])
                        elif fault == 'not_met':
                            decision.update(verdict='not_met', rationale='Authored incompatible-date judgment for this fixture.')
                    if fault == 'duplicate' and decisions:
                        decisions.append(copy.deepcopy(next(d for d in decisions if kinds[d['requirement_id']] == 'currentness')))
                    if fault == 'missing':
                        decisions = []
                    assessments.append(dict(candidate_id=c['candidate_id'], verdict='supported', in_scope=True,
                        rationale='The source supports the literal field values.', report_mappings=fixture_mappings(c, payload),
                        evidence_quotes=[c['proposal']['evidence'][0]['quote']], eligibility_assessments=decisions))
                return case.cli.response(text=json.dumps(dict(assessments=assessments)))
            if title == 'CoverageAssessment':
                return case.cli.response(text=json.dumps(dict(assessments=[dict(
                    criterion_id=r['criterion_id'], judgment='satisfied', items=[], target_count=r['target_count'],
                    result_goal_id=r['result_goal_id'], benchmark_candidate_ids=[],
                    evidence_candidate_ids=[c['candidate_id'] for c in payload['candidates']], gaps=[],
                    rationale='Deliberately optimistic fixture assessment.') for r in case.schema['completion_checks']],
                    scope_estimates=[], next_searches=[])))
            return original(request)

        case.flow.provider = provider
        _, directory, saved, _ = scenario.run(budget=25, search=False)
        return ResearchSession.model_validate(saved), directory, seen

    def test_unknown_currentness_preserves_facts_and_blocks_false_completion(self):
        """Keep true field claims while denying a blanket approval of current membership.

        Args:
            self: Test owner checking the real report, coverage and stopping consumers.
        Returns:
            None; facts and fields remain supported, entries stay pending, and the
            optimistic coverage reply cannot declare the research complete.
        """
        session, directory, seen = self.run_case('unknown')
        answer = json.loads((directory / 'answer.json').read_text())
        result = answer['progress']['goals'][0]['results']
        self.assertEqual((result['field_supported'], result['supported'], result['pending']), (2, 0, 2))
        self.assertTrue(all(c['adjudication_status'] == 'supported' for c in session.extraction['candidates']))
        self.assertTrue(all(r['qualification_status'] == 'unknown' for r in result['records']))
        self.assertFalse(completion_status(session, directory.parent)['ready'])
        _, valid = checked_candidates(session, directory.parent)
        ledger = coverage_results(session, valid)['G1']
        self.assertEqual(ledger['supported'], 0)
        self.assertEqual(ledger['items'][0]['qualification_status'], 'unknown')
        self.assertIn('inclusion unknown', '\n'.join(render_report(answer)))
        self.assertTrue(seen)
        self.assertIn('eligibility_assessments', str(seen[0]['output_config']['format']['schema']))
        payload = json.loads(seen[0]['messages'][0]['content'])
        self.assertTrue(any(r['kind'] == 'currentness' for r in payload['report_requirements']['G1']))

    def test_invalid_inclusion_decisions_do_not_erase_valid_factual_reviews(self):
        """Reject unsupported qualification while preserving independently valid facts.

        Args:
            self: Test owner varying attribution, evidence and completeness failures.
        Returns:
            None; every case remains pending with explicit missing/invalid receipts,
            while no factual review receives an eligibility-derived error code.
        """
        for fault in ('missing', 'quote', 'index', 'duplicate', 'timestamp', 'not_met'):
            with self.subTest(fault=fault):
                session, _, _ = self.run_case(fault)
                result = result_progress(session.model_dump(mode='json'))['goals'][0]['results']
                self.assertEqual((result['field_supported'], result['supported'], result['pending']), (2, 0, 2))
                self.assertTrue(all(not r['error_code'] for r in session.claim_reviews.values()))
                self.assertTrue(all(c['adjudication_status'] == 'supported' for c in session.extraction['candidates']))

    def test_valid_inclusion_and_contract_change_have_distinct_outcomes(self):
        """Require a new decision when the answer contract changes after review.

        Args:
            self: Test owner preserving receipts while changing an in-memory scope copy.
        Returns:
            None; positive source-bound judgments qualify both rows, but altered scope
            invalidates qualification and the completion freshness fingerprint.
        """
        session, _, _ = self.run_case()
        data = session.model_dump(mode='json')
        result = result_progress(data)['goals'][0]['results']
        self.assertEqual(result['supported'], 2)
        self.assertTrue(all(r['qualification_status'] == 'met' for r in result['records']))
        before = evidence_revision(session)
        data['answer_schema']['scope'] += ' A different inclusion boundary.'
        changed = ResearchSession.model_validate(data)
        self.assertNotEqual(before, evidence_revision(changed))
        self.assertEqual(result_progress(data)['goals'][0]['results']['supported'], 0)

    def test_legacy_reports_are_not_silently_given_eligibility_approval(self):
        """Keep historical field counts readable while explicitly labelling their limit.

        Args:
            self: Test owner using old-format claims with no inclusion receipts.
        Returns:
            None; legacy rows say eligibility was not assessed, and enabling the new
            policy without new evidence leaves them pending rather than grandfathered.
        """
        data = source_fixture()
        legacy = result_progress(data)['goals'][0]['results']
        self.assertEqual(legacy['supported'], 1)
        self.assertEqual(legacy['records'][0]['qualification_status'], 'not_assessed_legacy')
        data['workflow']['claim_support_policy'] = POLICY
        updated = result_progress(data)['goals'][0]['results']
        self.assertEqual((updated['field_supported'], updated['supported'], updated['pending']), (1, 0, 1))

    def test_split_contributions_combine_requirements_but_preserve_conflicts(self):
        """Use actual joined identities for complementary inclusion evidence.

        Args:
            self: Test owner supplying separate authored affiliation/topic decisions.
        Returns:
            None; unknown local evidence does not negate positive evidence elsewhere,
            but positive and negative judgments on the same requirement stay conflicting.
        """
        data = source_fixture()
        data['workflow']['claim_support_policy'] = POLICY
        data['answer_schema']['as_of'] = '2026-10-30'
        requirements = requirement_catalog(data['answer_schema'])['G1']
        data['claim_reviews'] = {}
        for n, c in enumerate(data['extraction']['candidates']):
            decisions = [dict(record_index=0, goal_id='G1', requirement_id=r['requirement_id'],
                record_sha256=record_fingerprint(c['proposal']['result_records'][0]),
                verdict='met' if i == n else 'unknown', rationale='Authored complementary evidence.',
                error_code=None, evidence=[dict(start=0, end=10, quote='Fixture evidence')], context={}) for i, r in enumerate(requirements)]
            data['claim_reviews'][c['candidate_id']] = dict(text_sha256=c['text_sha256'], error_code=None,
                                                         qualification_reviews=decisions)
        self.assertEqual(result_progress(data)['goals'][0]['results']['supported'], 1)
        data['claim_reviews']['C2']['qualification_reviews'][0]['verdict'] = 'not_met'
        result = result_progress(data)['goals'][0]['results']
        self.assertEqual(result['supported'], 0)
        self.assertEqual(result['records'][0]['qualification_status'], 'conflicting')

    def test_record_indices_are_candidate_global_and_decisions_bind_field_values(self):
        """Prevent another goal's record or changed field values from borrowing approval.

        Args:
            self: Test owner constructing one candidate with records for two goals.
        Returns:
            None; G1 uses its global record index one, and changing that record's
            content invalidates its old inclusion receipts even if the index stays one.
        """
        data = source_fixture()
        data['workflow']['claim_support_policy'] = POLICY
        c = data['extraction']['candidates'][0]
        c['proposal']['result_records'][0]['fields'].append(dict(name='value', value='Privacy'))
        other = copy.deepcopy(c['proposal']['result_records'][0])
        other['goal_id'] = 'G2'
        c['proposal']['result_records'].insert(0, other)
        data['extraction']['candidates'] = [c]
        requirements = requirement_catalog(data['answer_schema'])['G1']
        data['claim_reviews'] = {c['candidate_id']: dict(error_code=None, text_sha256=c['text_sha256'],
            qualification_reviews=[dict(record_index=1, goal_id='G1', requirement_id=r['requirement_id'],
                record_sha256=record_fingerprint(c['proposal']['result_records'][1]), verdict='met',
                rationale='Authored judgment for this record only.', error_code=None,
                evidence=[dict(start=0, end=10, quote='Fixture evidence')], context={}) for r in requirements])}
        self.assertEqual(result_progress(data)['goals'][0]['results']['supported'], 1)
        c['proposal']['result_records'][1]['fields'][-1]['value'] = 'Different focus'
        self.assertEqual(result_progress(data)['goals'][0]['results']['supported'], 0)
