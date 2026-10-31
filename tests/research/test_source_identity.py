"""Check conservative source-subject joins and real graph mapping-repair behavior.

Handwritten observations expose identity conflicts, reused local entity IDs and
source-integrity filtering. The CLI cases use synthetic HTTP/model responses but
execute the production graph, review, repair scheduling and report accounting.
These checks prove application rules, not a live model's semantic accuracy.
"""
import copy
import json
import unittest

from projectclarity.claim_review.report_mapping import closes_gap, mapping_assessments
from projectclarity.research_results import result_progress
from tests.research.test_record_aggregation import fixture, observation
from tests.research import test_report_mapping
from tests.research.adversarial_source_scenarios import SourceScenario
from tests.claim_review.mapping_fixtures import fixture_eligibility


def source_fixture():
    """Create separately supported affiliation and topic claims about one subject.

    Args:
        None.
    Returns:
        Serialized run data with composite identity and distinct donor/field quotes.
        No files, provider calls or source parsing are substituted by this helper.
    """
    data = fixture([observation('C1', dict(item='Researcher A', affiliation='Institute A')),
                    observation('C2', dict(item='Researcher A', value='Privacy'))])
    section = data['answer_schema']['sections'][0]
    section['fields'].append(dict(name='affiliation', meaning='Named institution',
        value_kind='organization_name', multiplicity='single', absence_meaning='Unknown institution'))
    section['identity_fields'] = ['item', 'affiliation']
    data['plan']['goals'][0]['result_shape']['required_fields'].append('affiliation')
    for candidate in data['extraction']['candidates']:
        candidate.update(source_id='S1', text_sha256='source-version', batch_id='S1_P1')
        candidate['proposal']['entity_ids'] = ['E1']
    return data


class SourceIdentityTests(unittest.TestCase):
    """Demand explicit same-batch linkage and preserve original field ownership."""

    def test_partial_fields_join_with_original_provenance_in_either_order(self):
        """Combine a subject's topic and affiliation without altering either claim.

        Args:
            self: Test owner comparing accounting and mapping consumers.
        Returns:
            None; asserts one complete entry, original field owners and no mutation.
        """
        for reverse in (False, True):
            data = source_fixture()
            if reverse:
                data['extraction']['candidates'].reverse()
            original = copy.deepcopy(data)
            result = result_progress(data)['goals'][0]['results']
            self.assertEqual((result['supported'], result['unassigned_count']), (1, 0))
            row = result['records'][0]
            self.assertEqual(row['field_status']['affiliation']['alternatives'][0]['candidate_ids'], ['C1'])
            self.assertEqual(row['field_status']['value']['alternatives'][0]['candidate_ids'], ['C2'])
            self.assertEqual(row['identity_associations'][0]['donor_candidate_ids'], ['C1'])
            self.assertEqual(data, original)
            data['claim_reviews'] = {'C2': dict(text_sha256='source-version', error_code=None,
                assessment=dict(report_mappings=[dict(goal_id='G1', status='mapped',
                    field_names=['item', 'value'], rationale='Topic contribution is present.'),
                    dict(goal_id='G99', status='mapped', field_names=['bad'], rationale='Unrelated extra judgment.')]))}
            candidate = next(c for c in data['extraction']['candidates'] if c['candidate_id'] == 'C2')
            self.assertEqual(mapping_assessments(data, candidate)[0]['status'], 'mapped')

    def test_ambiguous_or_untrusted_donors_never_fill_identity(self):
        """Reject names alone, reused IDs, conflicting affiliations and bad support.

        Args:
            self: Test owner applying one independent negative condition per case.
        Returns:
            None; partial topic remains unassigned under every invalid association.
        """
        for fault in ('source', 'version', 'batch', 'subject', 'unsupported', 'context', 'conflict', 'filtered'):
            with self.subTest(fault=fault):
                data = source_fixture()
                donor, partial = data['extraction']['candidates']
                if fault in {'source', 'version', 'batch'}:
                    partial[{'source': 'source_id', 'version': 'text_sha256', 'batch': 'batch_id'}[fault]] = 'different'
                elif fault == 'subject':
                    partial['proposal']['entity_ids'] = ['E2']
                elif fault == 'unsupported':
                    donor['adjudication_status'] = 'insufficient_evidence'
                elif fault == 'context':
                    donor['proposal']['research_roles'] = ['context']
                elif fault == 'conflict':
                    other = copy.deepcopy(donor)
                    other['candidate_id'] = 'C3'
                    other['proposal']['result_records'][0]['fields'][1]['value'] = 'Institute B'
                    data['extraction']['candidates'].append(other)
                result = result_progress(data, {'C2'} if fault == 'filtered' else None)['goals'][0]['results']
                self.assertEqual(result['unassigned_count'], 1)
                self.assertEqual(result['supported'], 0)

    def test_repair_continuity_preserves_qualifications_values_and_source(self):
        """Reject a useful-looking child when it changes the original evidence meaning.

        Args:
            self: Test owner varying one repair-continuity condition at a time.
        Returns:
            None; paraphrased structured repair passes, but altered identity values,
            dropped qualifications, changed source versions and noncovering quotes fail.
        """
        original = source_fixture()['extraction']['candidates'][0]
        original['evidence'] = [dict(start=10, end=30)]
        original['proposal']['qualifications'] = ['Historical affiliation only.']
        replacement = copy.deepcopy(original)
        replacement['proposal']['statement'] = 'A differently worded statement.'
        replacement['proposal']['result_records'][0]['fields'].append(dict(name='value', value='Privacy'))
        gap = dict(goal_id='G1', field_names=['value'])
        assessment = dict(goal_id='G1', status='mapped', field_names=['item', 'affiliation', 'value'])
        self.assertTrue(closes_gap(original, replacement, gap, assessment))
        for fault in ('value', 'qualification', 'source', 'anchor'):
            with self.subTest(fault=fault):
                child = copy.deepcopy(replacement)
                if fault == 'value':
                    child['proposal']['result_records'][0]['fields'][0]['value'] = 'Different person'
                elif fault == 'qualification':
                    child['proposal']['qualifications'] = []
                elif fault == 'source':
                    child['text_sha256'] = 'changed-source'
                else:
                    child['evidence'] = [dict(start=31, end=50)]
                self.assertFalse(closes_gap(original, child, gap, assessment))

    def test_repair_of_one_record_cannot_close_a_multiple_record_obligation(self):
        """Require continuity for every original record, not just the first match.

        Args:
            self: Test owner creating one claim that describes two requested people.
        Returns:
            None; one repaired person cannot discharge the other person's missing field.
        """
        original = source_fixture()['extraction']['candidates'][0]
        original['evidence'] = [dict(start=10, end=30)]
        other = copy.deepcopy(original['proposal']['result_records'][0])
        other['fields'][0]['value'] = 'Researcher B'
        original['proposal']['result_records'].append(other)
        child = copy.deepcopy(original)
        child['proposal']['result_records'] = child['proposal']['result_records'][:1]
        child['proposal']['result_records'][0]['fields'].append(dict(name='value', value='Privacy'))
        gap = dict(goal_id='G1', field_names=['value'])
        assessment = dict(goal_id='G1', status='mapped', field_names=['value'])
        self.assertFalse(closes_gap(original, child, gap, assessment))
        complete_other = copy.deepcopy(other)
        complete_other['fields'].append(dict(name='value', value='Robustness'))
        child['proposal']['result_records'].append(complete_other)
        self.assertTrue(closes_gap(original, child, gap, assessment))


class SourceRepairCLITests(unittest.TestCase):
    """Exercise changed mapping rules through the public CLI and actual graph."""

    run_mapping = test_report_mapping.ReportMappingTests.run_mapping

    def test_subject_join_through_cli_without_repaired_full_record(self):
        """Combine separate source facts through the real extraction-to-report route.

        Args:
            self: Test owner providing a literal three-field source and synthetic replies.
        Returns:
            None; a complete record must arise from separate supported contributions,
            never a fixture-provided complete record or a guessed name association.
        """
        scenario = SourceScenario(self, count=1, arrivals=0, concurrency=1)
        case = scenario.case
        section = case.schema['sections'][0]
        section['identity_fields'] = ['item', 'affiliation']
        section['fields'].append(dict(name='affiliation', meaning='Listed institution',
            value_kind='organization_name', multiplicity='single', absence_meaning='Unknown'))
        source = b'<main><table><tr><th>Item</th><th>Affiliation</th><th>Value</th></tr><tr><td>Researcher A</td><td>Institute A</td><td>Privacy</td></tr></table></main>'
        case.flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'}, source)
        original = case.provider

        def provider(request):
            """Supply separate claims while retaining the real passage and review input.

            Args:
                request: Actual SDK request from the public CLI graph execution.
            Returns:
                Synthetic provider response; no report inventory or joining code is mocked.
            """
            wire = json.loads(request.content)
            title = wire['output_config']['format']['schema']['title']
            payload = json.loads(wire['messages'][0]['content'])
            if title == 'ExtractionBatch' and 'Researcher A' in payload['passage']['text']:
                claims = []
                for cid, values, quote in [('C1', {'item': 'Researcher A', 'affiliation': 'Institute A'}, 'Institute A'),
                                           ('C2', {'item': 'Researcher A', 'value': 'Privacy'}, 'Privacy')]:
                    claims.append(dict(claim_id=cid, statement=f'Researcher A: {quote}', goal_ids=['G1'],
                        entity_ids=['E1'], relationship=None, claim_kind='non_relational', qualifications=[],
                        research_roles=['answer_evidence'], result_records=[dict(goal_id='G1',
                            fields=[dict(name=k, value=v) for k, v in values.items()])],
                        evidence=[dict(passage_id=payload['passage']['passage_id'], quote=quote)]))
                return case.cli.response(text=json.dumps(dict(entities=[dict(entity_id='E1',
                    name='Researcher A', entity_type='person')], claims=claims)))
            if title == 'ClaimReviewBatch':
                assessments = [dict(candidate_id=c['candidate_id'], verdict='supported', in_scope=True,
                    eligibility_assessments=fixture_eligibility(c, payload),
                    rationale='The supplied record states this fact.', evidence_quotes=[c['proposal']['evidence'][0]['quote']],
                    report_mappings=[dict(goal_id=r['goal_id'], status='mapped', rationale='This contribution is present.',
                        field_names=[f['name'] for f in r['fields']]) for r in c['proposal']['result_records']])
                    for c in payload['candidates']]
                return case.cli.response(text=json.dumps(dict(assessments=assessments)))
            return original(request)

        case.flow.provider = provider
        _, directory, saved, _ = scenario.run(budget=25, search=False)
        answer = json.loads((directory / 'answer.json').read_text())
        result = answer['progress']['goals'][0]['results']
        self.assertEqual((result['identified'], result['supported'], result['unassigned_count']), (1, 1, 0))
        self.assertEqual(result['records'][0]['values'], dict(item='Researcher A', affiliation='Institute A', value='Privacy'))
        self.assertTrue(result['records'][0]['identity_associations'])
        self.assertTrue(all(len(r['fields']) == 2 for c in saved['extraction']['candidates']
                            for r in c['proposal']['result_records']))
        self.assertEqual(answer['progress']['mapping_ledger']['unresolved'], 0)

    def test_partial_repair_paraphrase_closes_after_review(self):
        """Recognize a reworded repair through source anchors and retained values.

        Args:
            self: Test owner retaining isolated graph-run artifacts.
        Returns:
            None; all five repaired records appear and their original obligations close.
        """
        _, session, _, _ = self.run_mapping(fault='partial_paraphrase')
        progress = result_progress(session.model_dump(mode='json'))
        self.assertEqual(progress['goals'][0]['results']['supported'], 5)
        self.assertEqual(progress['mapping_ledger']['unresolved'], 0)
        self.assertEqual(sum(r['status'] == 'resolved' for r in progress['mapping_ledger']['rows']), 5)

    def test_context_cannot_create_repairs_even_when_reviewer_requests_them(self):
        """Reject a false mapping obligation without discarding the background claim.

        Args:
            self: Test owner returning deliberately over-demanding reviewer judgments.
        Returns:
            None; no report rows, mapping blockers or queued mapping repairs appear.
        """
        _, session, _, _ = self.run_mapping(fault='context_wrong_mapping')
        progress = result_progress(session.model_dump(mode='json'))
        self.assertEqual(progress['goals'][0]['results']['identified'], 0)
        self.assertEqual(progress['mapping_ledger']['unresolved'], 0)
        self.assertFalse(session.workflow['report_mapping_repairs'])
