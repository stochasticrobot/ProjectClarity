"""Check how source observations combine into designed-report entries.

Fixtures cover composite identity, missing identity, changing attributes, single
versus multiple values, contradictions and source-integrity filtering. Tests compare
report, coverage and dashboard consumers, including the JavaScript renderer, and
exercise the public command with synthetic provider replies. They establish the
asserted grouping behavior, not real-world entity resolution.
"""
import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from projectclarity.answer_schema.contracts import AnswerSchema
from projectclarity.answer_schema.completion import report_blockers
from projectclarity.answer_schema.render import render_report
from projectclarity.coverage.contract_assessment import compact_ledgers
from projectclarity.coverage.result_inventory import coverage_results
from projectclarity.coverage.stopping import evidence_revision
from projectclarity.dashboard.targets import target_groups
from projectclarity.research_results import result_progress
from projectclarity.nodes.schedule_work.answer_controller import POLICY
from tests.research.adversarial_source_scenarios import SourceScenario
from tests.research.test_contract_completion import explicit_specification
from tests.research.research_test_fixtures import example_session


def observation(cid, values, verdict='supported', role='answer_evidence'):
    """Create an anchored generic observation without an entity-type heuristic.

    Args:
        cid: Unique candidate identifier.
        values: Supplied field values; missing values are intentionally not filled.
        verdict: Recorded evidence judgment.
        role: Evidence role, distinct from a contradiction judgment.
    Returns:
        Candidate dictionary with independently traceable evidence for projections.
    """
    return dict(candidate_id=cid, evidence_status='anchored', adjudication_status=verdict,
        evidence=[dict(source_id=cid + '_source', quote=json.dumps(values))],
        proposal=dict(goal_ids=['G1'], statement=json.dumps(values), qualifications=[],
            research_roles=[role], claim_kind='non_relational', entity_ids=[], relationship=None,
            result_records=[dict(goal_id='G1', fields=[dict(name=k, value=v) for k, v in values.items()])]))


def fixture(candidates, multiplicity='single'):
    """Build shared accounting input with an explicit one-record answer requirement.

    Args:
        candidates: Independent observations, never preaggregated expected records.
        multiplicity: Contract property for the descriptive value field.
    Returns:
        Serialized input accepted by result_progress and projection consumers.
    """
    schema = explicit_specification(1)
    schema['sections'][0]['fields'][1]['multiplicity'] = multiplicity
    return dict(answer_schema=schema, workflow=dict(controller_policy=POLICY), plan=dict(goals=[dict(goal_id='G1',
        question='Identify the requested items and values.', result_shape=dict(
            unit='source items', required_fields=['item', 'value']))]),
        extraction=dict(candidates=candidates))


class RecordAggregationTests(unittest.TestCase):
    """Verify identity, evidence and field cardinality remain separate concepts."""

    def test_complete_identity_groups_without_person_assumptions(self):
        """Exercise products, events, organizations and directed relationship keys.

        Args:
            self: Assertion owner with fresh observations per case.
        Returns:
            None; equivalent identities combine, different exact keys stay separate.
        """
        for name in ('Widget X', 'Launch 2026', 'Research Institute', 'A funds B'):
            with self.subTest(name=name):
                rows = [observation('C1', dict(item=name, value='Alpha')),
                        observation('C2', dict(item=name, value='Beta')),
                        observation('C3', dict(item=name + ' II', value='Alpha'))]
                result = result_progress(fixture(rows, 'multiple'))['goals'][0]['results']
                self.assertEqual((result['identified'], result['supported']), (2, 2))
                self.assertEqual(result['records'][0]['candidate_ids'], ['C1', 'C2'])
                self.assertEqual(len(result['records'][0]['provenance']), 2)

    def test_unassigned_observations_never_count_or_fill_known_records(self):
        """Retain partial evidence separately without inventing missing identity.

        Args:
            self: Assertion owner exercising shared report and coverage projections.
        Returns:
            None; unassigned values remain inspectable and cannot complete a row.
        """
        data = fixture([observation('C1', dict(item='Widget')),
                        observation('C2', dict(value='Feature'))])
        progress = result_progress(data)
        result = progress['goals'][0]['results']
        self.assertEqual((result['identified'], result['supported'], result['incomplete']), (1, 0, 1))
        self.assertEqual(len(result['records']), 1)
        self.assertNotIn('value', result['records'][0]['values'])
        self.assertEqual(result['unassigned_count'], 1)
        unassigned = result['unassigned_observations'][0]
        self.assertEqual(unassigned['candidate_ids'], ['C2'])
        self.assertEqual(unassigned['provenance'][0]['source_id'], 'C2_source')
        targets = target_groups(progress)
        self.assertEqual(len(targets[0]['rows']), 1)
        self.assertEqual(targets[0]['unassigned_observations'], [unassigned])
        self.assertTrue(report_blockers(SimpleNamespace(answer_schema=data['answer_schema']), {'G1': result}))

    def test_composite_identity_and_attribute_updates_keep_stable_records(self):
        """Group directed pairs by all declared keys, not names or arrival order.

        Args:
            self: Assertion owner constructing a generic relationship contract.
        Returns:
            None; attribute enrichment keeps identity, reversed pairs remain separate,
            and partial keys remain unassigned instead of joining by one label.
        """
        data = fixture([observation('C1', dict(item='A', recipient='B'))])
        section = data['answer_schema']['sections'][0]
        section['fields'].append(dict(name='recipient', meaning='Recipient of the relationship',
            value_kind='text', multiplicity='single', absence_meaning='Unknown'))
        section['identity_fields'] = ['item', 'recipient']
        data['plan']['goals'][0]['result_shape']['required_fields'].append('recipient')
        AnswerSchema.model_validate(data['answer_schema'])
        before = result_progress(data)['goals'][0]['results']
        data['extraction']['candidates'].extend([
            observation('C2', dict(item='A', recipient='B', value='Grant')),
            observation('C3', dict(item='B', recipient='A', value='Grant')),
            observation('C4', dict(item='A', value='Different grant'))])
        after = result_progress(data)['goals'][0]['results']
        self.assertEqual((after['identified'], after['supported'], after['unassigned_count']), (2, 2, 1))
        self.assertEqual(before['records'][0]['result_id'], after['records'][0]['result_id'])
        self.assertEqual(after['records'][0]['candidate_ids'], ['C1', 'C2'])
        self.assertEqual(after['records'][0]['display_values']['value'], ['Grant'])

    def test_integrity_filter_and_invalid_fields_cannot_supply_support(self):
        """Keep changed-source evidence and malformed records from manufacturing rows.

        Args:
            self: Assertion owner supplying explicit source-integrity eligibility.
        Returns:
            None; filtered observations do not complete records, malformed data is
            retained separately with errors and cannot inflate supported counts.
        """
        data = fixture([observation('C1', dict(item='Widget')),
                        observation('C2', dict(item='Widget', value='Feature')),
                        observation('C3', dict(item='Other', value='Feature', unknown='Invalid'))])
        result = result_progress(data, {'C1', 'C3'})['goals'][0]['results']
        self.assertEqual((result['identified'], result['supported'], result['incomplete']), (1, 0, 1))
        self.assertNotIn('value', result['records'][0]['values'])
        self.assertEqual(result['unassigned_count'], 1)
        self.assertEqual(result['unassigned_observations'][0]['status'], 'invalid')
        self.assertIn('unknown_result_field', result['unassigned_observations'][0]['errors'])

    def test_single_alternatives_remain_pending_in_any_observation_order(self):
        """Reject false disputes while preserving the ambiguity completion gate.

        Args:
            self: Assertion owner reversing source arrival order.
        Returns:
            None; alternatives remain visible, pending and completion-blocking.
        """
        rows = [observation('C1', dict(item='Widget', value='Alpha')),
                observation('C2', dict(item='Widget', value='Beta'))]
        for candidates in (rows, list(reversed(rows))):
            data = fixture(candidates)
            result = result_progress(data)['goals'][0]['results']
            self.assertEqual((result['pending'], result['disputed'], result['supported']), (1, 0, 0))
            self.assertEqual(set(result['records'][0]['display_values']['value']), {'Alpha', 'Beta'})
            self.assertTrue(report_blockers(SimpleNamespace(answer_schema=data['answer_schema']), {'G1': result}))

    def test_multiple_values_preserve_support_and_unsupported_alternatives(self):
        """Collect additive supported strings without blessing pending observations.

        Args:
            self: Assertion owner checking provenance and field support separately.
        Returns:
            None; supported values coexist, pending values remain retained but unapproved.
        """
        data = fixture([observation('C1', dict(item='Widget', value='Alpha')),
                        observation('C2', dict(item='Widget', value='Beta')),
                        observation('C3', dict(item='Widget', value='Maybe'), 'pending')], 'multiple')
        result = result_progress(data)['goals'][0]['results']
        row = result['records'][0]
        self.assertEqual(result['supported'], 1)
        self.assertEqual(row['display_values']['value'], ['Alpha', 'Beta'])
        self.assertEqual(len(row['field_status']['value']['alternatives']), 3)
        self.assertEqual(row['field_status']['value']['supported_values'], ['Alpha', 'Beta'])
        self.assertEqual(len(row['provenance']), 3)
        pending = result_progress(fixture([data['extraction']['candidates'][2]], 'multiple'))['goals'][0]['results']
        self.assertEqual((pending['supported'], pending['pending']), (0, 1))

    def test_explicit_contradiction_and_counterevidence_are_not_conflated(self):
        """Dispute explicit contradictions, but keep unresolved counterevidence pending.

        Args:
            self: Assertion owner testing both cardinalities and evidence verdicts.
        Returns:
            None; neither kind of adverse evidence can silently permit completion.
        """
        for mode in ('single', 'multiple'):
            for verdict, expected in (('supported', 'pending'), ('contradicted', 'disputed')):
                with self.subTest(mode=mode, verdict=verdict):
                    data = fixture([observation('C1', dict(item='Widget', value='Alpha')),
                        observation('C2', dict(item='Widget', value='Beta'), verdict, 'counterevidence')], mode)
                    result = result_progress(data)['goals'][0]['results']
                    self.assertEqual((result[expected], result['supported']), (1, 0))
                    self.assertTrue(report_blockers(SimpleNamespace(answer_schema=data['answer_schema']), {'G1': result}))

    def test_schema_defaults_and_identity_multiplicity_boundaries(self):
        """Keep old schemas conservative and reject ambiguous identity cardinality.

        Args:
            self: Assertion owner validating the actual provider-output model.
        Returns:
            None; legacy fields default single and invalid contracts fail validation.
        """
        schema = explicit_specification(1)
        for field in schema['sections'][0]['fields']:
            field.pop('multiplicity')
        checked = AnswerSchema.model_validate(schema)
        self.assertEqual([f.multiplicity for f in checked.sections[0].fields], ['single', 'single'])
        for field_index, value in ((0, 'multiple'), (1, 'arbitrary')):
            invalid = copy.deepcopy(schema)
            invalid['sections'][0]['fields'][field_index]['multiplicity'] = value
            with self.assertRaises(ValueError):
                AnswerSchema.model_validate(invalid)

    def test_coverage_markdown_and_dashboard_share_the_full_inventory(self):
        """Exercise all report consumers, including the actual browser renderer.

        Args:
            self: Assertion owner running local JavaScript without network or panels.
        Returns:
            None; all supported values and separate unassigned citations survive.
        """
        data = fixture([observation('C1', dict(item='Widget', value='Alpha')),
                        observation('C2', dict(item='Widget', value='Beta')),
                        observation('C3', dict(value='<unassigned>'))], 'multiple')
        before = copy.deepcopy(data)
        progress = result_progress(data)
        session = SimpleNamespace(model_dump=lambda **kwargs: copy.deepcopy(data))
        ledger = compact_ledgers(coverage_results(session, {'C1', 'C2', 'C3'}))['G1']
        self.assertEqual(ledger['items'][0]['display_values']['value'], ['Alpha', 'Beta'])
        self.assertEqual(ledger['unassigned_count'], 1)
        self.assertEqual(ledger['unassigned_observations'][0]['candidate_ids'], ['C3'])
        markdown = '\n'.join(render_report(dict(answer_schema=data['answer_schema'], progress=progress)))
        self.assertIn('Alpha; Beta', markdown)
        self.assertIn('Unassigned evidence', markdown)
        self.assertIn('[C3]', markdown)
        view = dict(answer_schema=data['answer_schema'], targets=target_groups(progress),
                    result_progress=progress, claims=[])
        check = subprocess.run(['node', 'tests/dashboard/report_checks.js'], input=json.dumps(view),
            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertEqual(data, before, 'Projection must not rewrite saved observations.')

    def test_aggregation_rules_and_cardinality_invalidate_old_completion(self):
        """Prevent stale completion receipts from surviving changed aggregation rules.

        Args:
            self: Assertion owner fingerprinting the production evidence revision.
        Returns:
            None; changed cardinality or aggregation policy invalidates freshness.
        """
        session = example_session()
        session.answer_schema = explicit_specification(1)
        original = evidence_revision(session)
        with patch('projectclarity.answer_schema.inventory.AGGREGATION_VERSION', 'different-rules'):
            self.assertNotEqual(evidence_revision(session), original)
        session.answer_schema['sections'][0]['fields'][1]['multiplicity'] = 'multiple'
        self.assertNotEqual(evidence_revision(session), original)

    def test_public_cli_obeys_single_and_multiple_completion_semantics(self):
        """Read real source rows through production orchestration and compare contracts.

        Args:
            self: Fixture owner replacing only external HTTP and model transports.
        Returns:
            None; multiple values complete one row, unresolved single values block
            automatic success. This does not prove live provider or model quality.
        """
        for mode in ('single', 'multiple'):
            with self.subTest(mode=mode):
                scenario = SourceScenario(self, count=1, arrivals=0)
                scenario.case.schema['sections'][0]['fields'][1]['multiplicity'] = mode
                body = ('<html><body><table><tr><th>Item</th><th>Value</th></tr>'
                        '<tr><td>Entry0000</td><td>FeatureAlpha</td></tr>'
                        '<tr><td>Entry0000</td><td>FeatureBeta</td></tr></table></body></html>')
                scenario.case.flow.routes[('example.org', '/research')] = (
                    200, {'Content-Type': 'text/html'}, body.encode())
                _, directory, saved, diagnostics = scenario.run(budget=40, search=False)
                answer = json.loads((directory / 'answer.json').read_text())
                result = answer['progress']['goals'][0]['results']
                self.assertEqual(len(result['records']), 1, diagnostics)
                row = result['records'][0]
                self.assertEqual(set(row['display_values']['value']), {'FeatureAlpha', 'FeatureBeta'})
                self.assertGreaterEqual(len(row['candidate_ids']), 2)
                self.assertEqual(result['disputed'], 0)
                if mode == 'multiple':
                    self.assertEqual(result['supported'], 1)
                    self.assertEqual(saved['workflow']['stop_reason'], 'criteria_satisfied', diagnostics)
                else:
                    self.assertEqual(result['pending'], 1)
                    self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
