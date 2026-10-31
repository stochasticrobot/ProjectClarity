"""Check source provenance and reading priority for documents containing requested lists.

Component tests separate evidence metadata from item identity and inspect table
sampling on frozen pages. Command-line tests use two generic synthetic sources to
check whether actual item sections receive reading before distracting material
consumes the allowance. Saved records and pending reviews remain visible at tight
limits. Controlled replies establish mechanics, not live extraction recall.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import copy
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from projectclarity.extraction.document_structure import section_probes, passage_order
from projectclarity.extraction.saved_source_passages import iter_passages
from projectclarity.research_schemas import ResearchPlan
from projectclarity.result_contract import separate_plan_provenance
from projectclarity.research_results import record_check, result_inventory, result_defects
from projectclarity.retrieval.document_text_extractor import parse_document
from projectclarity.retrieval.retrieval_limits import RetrievalLimits
from tests.research.research_test_fixtures import example_session
from tests.research import test_workflow_control_cli as workflow_tests


class ProvenanceContractTests(unittest.TestCase):
    """Do not ask a model to invent citation metadata as part of item identity."""

    def test_new_plan_separates_provenance_but_preserves_document_identity_fields(self):
        """Keep original requirements in receipts, retaining legitimate URL/date fields.

        Args:
            self: Assertion owner using a generic document-enumeration plan.
        Returns:
            None; validates item fields, empty identities and unchanged input content.
        """
        plan = example_session().plan
        plan.goals[0].result_shape = {'unit': 'documents', 'required_fields':
            ['title', 'document_url', 'publication_date', 'source_citation', 'source_id', 'source_url']}
        plan = ResearchPlan.model_validate(plan.model_dump())
        receipts = separate_plan_provenance(plan)
        self.assertEqual(plan.goals[0].result_shape.required_fields, ['title', 'document_url', 'publication_date'])
        self.assertEqual(receipts[0]['provenance_fields'], ['source_citation', 'source_id', 'source_url'])
        self.assertEqual(len(receipts[0]['original_shape']['required_fields']), 6)
        self.assertEqual(separate_plan_provenance(plan), [])
        plan.goals[0].result_shape.required_fields = ['source_citation']
        self.assertEqual(separate_plan_provenance(plan)[0]['status'], 'item_identity_undefined')
        self.assertIsNone(plan.goals[0].result_shape)

    def test_legacy_records_use_anchored_provenance_not_model_citations_or_tuple_identity(self):
        """Count repeated evidence for one item once without joining missing item fields.

        Args:
            self: Assertion owner with an old plan containing a provenance requirement.
        Returns:
            None; complete records retain real evidence while unsupported item fields stay absent.
        """
        goal = {'goal_id': 'G1', 'result_shape': {'unit': 'items',
            'required_fields': ['name', 'location', 'source_citation']}}
        record = {'goal_id': 'G1', 'fields': [{'name': 'name', 'value': 'A'}, {'name': 'location', 'value': 'B'}]}
        claim = {'goal_ids': ['G1'], 'claim_kind': 'non_relational', 'entity_ids': [],
            'research_roles': ['answer_evidence'], 'qualifications': [], 'result_records': [record]}
        candidate = {'candidate_id': 'S1_P1_C1', 'proposal': claim, 'adjudication_status': 'supported',
            'evidence': [{'source_id': 'S1', 'quote': 'A at B', 'start': 0, 'end': 6}]}
        other = copy.deepcopy(candidate)
        other['candidate_id'] = 'S2_P1_C1'
        other['evidence'][0]['source_id'] = 'S2'
        other['proposal']['result_records'][0]['fields'].append({'name': 'source_citation', 'value': 'invented'})
        result = result_inventory(goal, [candidate, other])
        self.assertEqual((result['supported'], result['incomplete']), (1, 0))
        self.assertEqual(len(result['records'][0]['provenance']), 2)
        self.assertNotIn('invented', json.dumps(result))
        self.assertEqual(result_defects(claim, [goal]), [])
        record['fields'].pop()
        self.assertEqual(record_check(record, goal)['missing_fields'], ['location'])

    def test_only_metadata_cannot_become_an_item(self):
        """Reject a legacy shape with no actual identifying item fields.

        Args:
            self: Assertion owner for a metadata-only shape.
        Returns:
            None; an empty tuple never becomes a complete supported result.
        """
        result = record_check({'goal_id': 'G', 'fields': []},
            {'goal_id': 'G', 'result_shape': {'unit': 'items', 'required_fields': ['source_id']}})
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('item_identity_undefined', result['errors'])


class SavedRosterProbeTests(unittest.TestCase):
    """Verify structural sampling on actual pages independently of expected MP answers."""

    def test_both_rosters_probe_beyond_introductions_without_discarding_passages(self):
        """Use record structure, not a roster URL or benchmark names, for first sampling.

        Args:
            self: Assertion owner; only public frozen HTML and production parsing are read.
        Returns:
            None; verifies exact text coverage and a non-prefix first extraction probe.
        """
        for name in ('house_of_commons', 'wikipedia'):
            with self.subTest(page=name):
                source = Path('benchmarks/canadian_parliament') / (name + '.html')
                text, _ = parse_document(source.read_bytes(), 'text/html', RetrievalLimits())
                attempts = [{'batch_id': f'S1_{pid}', 'source_id': 'S1', 'text_sha256': 'hash',
                    'start': start, 'end': end, 'status': 'not_attempted', 'refinement_parent': None}
                    for pid, start, end in iter_passages(text)]
                probes = section_probes(text, attempts)
                session = example_session()
                session.plan.goals[0].result_shape = {'unit': 'items', 'required_fields': ['name']}
                session.plan = ResearchPlan.model_validate(session.plan.model_dump())
                session.workflow['document_structure'] = {'S1': {'text_sha256': 'hash', 'probes': probes}}
                ordered = passage_order(session, attempts)
                self.assertEqual(ordered[0]['batch_id'], probes[0]['batch_id'])
                self.assertGreater(ordered[0]['start'], attempts[1]['end'])
                self.assertEqual({a['batch_id'] for a in ordered}, {a['batch_id'] for a in attempts})
                self.assertEqual(''.join(text[a['start']:a['end']] for a in attempts), text)
                session.workflow['document_structure']['S1']['text_sha256'] = 'stale'
                self.assertEqual(passage_order(session, attempts)[0]['batch_id'], 'S1_P1')


class CompetingDocumentsCLITests(unittest.TestCase):
    """Require item-rich late sections to displace a long supporting document."""

    def setUp(self):
        """Build two unseen generic sources and preserve all production application nodes.

        Args:
            self: Test fixture owning temporary files and synthetic transport boundaries.
        Returns:
            None; no network or paid model requests are made.
        """
        self.flow = workflow_tests.WorkflowControlCLITests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.base_provider = self.flow.provider
        self.flow.provider = self.provider
        self.cli = self.flow.cli
        self.cli.urls.write_text('https://example.org/research\nhttps://example.org/other\n')
        self.cli.plan['goals'][0].update(question='List each item and its location.',
            result_shape={'unit': 'items', 'required_fields': ['name', 'location', 'source_citation', 'source_id']})
        self.cli.plan['goals'][1].update(question='Describe supporting events.',
            evidence_required=['Dated source records.'], result_shape=None)
        self.calls = []
        self.review_payloads = []
        self.body = '<table><tr><th>Event</th><th>Date</th></tr>' + ''.join(
            f'<tr><td>Event{i}</td><td>2026</td></tr>' for i in range(160)) + '</table>'
        intro = '<table><tr><th>Former label</th><th>New label</th></tr>' + ''.join(
            f'<tr><td>Old{i}</td><td>New{i}</td></tr>' for i in range(40)) + '</table>'
        roster = '<table><tr><th>Name</th><th>Location</th></tr>' + ''.join(
            f'<tr><td>Item{i:03d}</td><td>Place{i:03d}</td></tr>' for i in range(64)) + '</table>'
        self.flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'}, self.body.encode())
        self.flow.routes[('example.org', '/other')] = (200, {'Content-Type': 'text/html'}, (intro + roster).encode())

    def provider(self, request):
        """Return source-derived fixture claims through the actual SDK request boundary.

        Args:
            self: Fixture recording actual production model inputs and call order.
            request: Provider request created by the unmodified application SDK adapter.
        Returns:
            A synthetic structured reply. Expected item lists are not read by the fixture.
        Review replies include authored report-mapping judgments; these do not
        establish live semantic recognition accuracy.
        """
        wire = json.loads(request.content)
        title = wire['output_config']['format']['schema']['title']
        if title == 'ResearchPlan':
            return self.cli.response()
        payload = json.loads(wire['messages'][0]['content'])
        if title == 'ExtractionBatch':
            self.calls.append((payload['source_id'], payload['passage']['passage_id']))
            fields = payload['goals'][0]['result_shape']['required_fields']
            self.assertEqual(fields, ['name', 'location'])
            text, pid = payload['passage']['text'], payload['passage']['passage_id']
            claims, entities = [], []
            for i, match in enumerate(re.finditer(r'Name: (Item\d+) \| Location: (Place\d+)', text)):
                subject, obj = f'E{i}', f'L{i}'
                entities.extend([{'entity_id': subject, 'name': match[1], 'entity_type': 'other'},
                    {'entity_id': obj, 'name': match[2], 'entity_type': 'place'}])
                claims.append({'claim_id': f'C{i}', 'statement': match.group(), 'goal_ids': ['G1'],
                    'entity_ids': [subject, obj], 'relationship': {
                        'subject_id': subject, 'predicate': 'located_in', 'object_id': obj}, 'claim_kind': 'relational',
                    'qualifications': [], 'research_roles': ['answer_evidence'],
                    'result_records': [{'goal_id': 'G1', 'fields': [
                        {'name': 'name', 'value': match[1]}, {'name': 'location', 'value': match[2]}]}],
                    'evidence': [{'passage_id': pid, 'quote': match.group()}]})
            if not claims and (match := re.search(r'Event: Event\d+ \| Date: 2026', text)):
                claims.append({'claim_id': 'C1', 'statement': match.group(), 'goal_ids': ['G1', 'G2'],
                    'entity_ids': [], 'relationship': None, 'claim_kind': 'non_relational',
                    'qualifications': [], 'research_roles': ['answer_evidence', 'context'],
                    'result_records': [], 'evidence': [{'passage_id': pid, 'quote': match.group()}]})
            return self.cli.response(text=json.dumps({'entities': entities, 'claims': claims}))
        if title == 'ClaimReviewBatch':
            self.review_payloads.append(payload)
            return self.cli.response(text=json.dumps({'assessments': [
                {'candidate_id': c['candidate_id'], 'verdict': 'supported', 'in_scope': True,
                 'rationale': 'Fixture support for original source fields.',
                 'report_mappings': fixture_mappings(c, payload), 'eligibility_assessments': fixture_eligibility(c, payload), 'evidence_quotes': [c['proposal']['evidence'][0]['quote']]} for c in payload['candidates']]}))
        return self.base_provider(request)

    def test_production_prioritizes_roster_and_retains_all_pairs_under_shared_budget(self):
        """Read actual item sections before a distracting source consumes the allowance.

        Args:
            self: Two documents with a misleading G1 background signal in the first.
        Returns:
            None; verifies saved complete pairs, review inputs, provenance and bounded
            calls, including the additional final coverage opportunity. All base
            roster passages must finish; optional refinements may remain queued
            after every expected pair has already been extracted and supported.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, directory, saved, _ = self.flow.run_workflow('--search', '--max-search-queries', '1',
                '--explore-before-search-depth', '1', '--max-requests', '29')
        answer = json.loads((directory / 'answer.json').read_text())
        result = answer['progress']['goals'][0]['results']
        expected = {(f'Item{i:03d}', f'Place{i:03d}') for i in range(64)}
        observed = {(r['values']['name'], r['values']['location']) for r in result['records'] if r['status'] == 'supported'}
        self.assertEqual(observed, expected)
        self.assertTrue(all(r['provenance'] for r in result['records']))
        self.assertTrue(saved['workflow']['result_contract_adjustments'])
        self.assertLessEqual(saved['usage']['requests_attempted'], 29)
        self.assertFalse(any(call['task'] == 'bounded_evidence' for call in saved['model_attempts']))
        self.assertEqual(saved['model_attempts'][-1]['task'], 'bounded_coverage')
        roster = [a for a in saved['extraction']['attempts'] if a['source_id'] == 'S2']
        self.assertTrue(all(a['status'] == 'completed' for a in roster
                            if a['start'] >= 2000 and not a['refinement_parent']))
        self.assertLess(sum(sid == 'S1' for sid, _ in self.calls), 10)
        self.assertTrue(self.review_payloads)

    def test_tighter_budget_retains_all_roster_pairs_with_unreviewed_tail(self):
        """The final coverage reservation cannot erase results awaiting claim review.

        Args:
            self: Small-profile competing-document fixture with one fewer model call.
        Returns:
            None. Assert all 64 pairs remain in the final answer, eight explicitly
            pending, and coverage runs instead of silently approving the unreviewed tail.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, directory, saved, _ = self.flow.run_workflow('--search', '--max-search-queries', '1',
                '--explore-before-search-depth', '1', '--max-requests', '28',
                '--extraction-profile', 'small')
        answer = json.loads((directory / 'answer.json').read_text())
        result = answer['progress']['goals'][0]['results']
        expected = {(f'Item{i:03d}', f'Place{i:03d}') for i in range(64)}
        self.assertEqual({(r['values']['name'], r['values']['location']) for r in result['records']}, expected)
        self.assertEqual((result['supported'], result['pending']), (56, 8))
        self.assertTrue(all(r['provenance'] for r in result['records']))
        self.assertEqual(saved['model_attempts'][-1]['task'], 'bounded_coverage')
        self.assertEqual(saved['usage']['requests_attempted'], 28)
        self.assertEqual(answer['coverage_freshness']['status'], 'fresh')
