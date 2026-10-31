"""Check result counting for people, events, funding records and relationships.

Handwritten observations exercise grouping, field completeness, review status and
target totals without assuming every answer is a list of people. Command-line
cases also check reporting and completion against large synthetic inventories.
Provider replies are controlled; these tests do not prove live extraction quality.
"""
import copy
import json
import unittest

from projectclarity.coverage.coverage_contracts import CoverageAssessment, CriterionAssessment
from projectclarity.nodes.assess_research_gaps.node import validate_assessment
from projectclarity.dashboard.targets import target_groups
from projectclarity.research_results import result_progress
from tests.research import test_result_accounting as accounting_fixture
from tests.research import test_result_reporting_cli as reporting_fixture
from tests.research.research_test_fixtures import example_session


class GeneralResultTests(unittest.TestCase):
    """Prevent topic-specific fixes and silent evidence-role exclusions."""

    def case(self, fields, rows):
        """Build independent source-supported records for an arbitrary answer shape.

        Args:
            self: Test owner using the existing minimal observation fixture.
            fields: Required fields selected by a hypothetical research plan.
            rows: Literal values to turn into complete records.
        Returns:
            Run dictionary; no providers, source reads or shared data are used.
        """
        f = accounting_fixture.ResultAccountingTests()
        f.setUp()
        f.goal['result_shape'] = dict(unit='research results', required_fields=fields)
        candidates = []
        for index, values in enumerate(rows):
            c = copy.deepcopy(f.candidate)
            c['candidate_id'] = f'S1_P1_C{index}'
            c['proposal']['result_records'][0]['fields'] = [dict(name=k, value=v) for k, v in zip(fields, values)]
            candidates.append(c)
        return dict(plan=dict(goals=[f.goal]), extraction=dict(candidates=candidates))

    def test_dual_role_support_survives_but_contradiction_does_not(self):
        """Evidence that refutes another claim can still answer the question.

        Args:
            self: Assertion owner using a non-person funding record.
        Returns:
            None; dual-role support counts, actual rejection is disputed and excluded.
        """
        data = self.case(['funder', 'recipient'], [('Foundation A', 'Institute B')])
        c = data['extraction']['candidates'][0]
        c['proposal']['research_roles'] = ['answer_evidence', 'counterevidence']
        result = result_progress(data)['goals'][0]['results']
        self.assertEqual((result['identified'], result['supported']), (1, 1))
        c['adjudication_status'] = 'contradicted'
        result = result_progress(data)['goals'][0]['results']
        self.assertEqual((result['identified'], result['supported'], result['disputed']), (0, 0, 1))

    def test_person_formatting_requires_all_other_fields_to_match(self):
        """Title/order variants merge, but same-name people with different context do not.

        Args:
            self: Generic researchers, not parliamentary names or field rules.
        Returns:
            None; assert two unresolved identity groups, preserved observations and stable IDs.
        """
        data = self.case(['full_name', 'organization'], [
            ('Dr. Ada Example', 'North Lab'), ('Example, Ada', 'North Lab'), ('Ada Example', 'South Lab')])
        data['extraction']['attempts'] = [dict(result=dict(entities=[
            dict(name='Ada Example', entity_type='person')]))]
        before = copy.deepcopy(data)
        first = result_progress(data)
        self.assertEqual(first['goals'][0]['results']['identified'], 0)
        self.assertEqual(first['goals'][0]['results']['identity_pending'], 2)
        self.assertEqual(len(first['goals'][0]['results']['records'][0]['field_variants']), 2)
        self.assertEqual(target_groups(first)[0]['identity_pending'], 2)
        data['extraction']['candidates'].reverse()
        self.assertEqual({r['result_id'] for r in first['goals'][0]['results']['records']},
            {r['result_id'] for r in result_progress(data)['goals'][0]['results']['records']})
        data['extraction']['candidates'].reverse()
        self.assertEqual(data, before)

    def test_organization_names_are_not_reversed_as_person_names(self):
        """Explicit organization typing overrides a generic full_name field convention.

        Args:
            self: Two differently named organizations sharing a location.
        Returns:
            None; punctuation/order differences remain distinct organizational names.
        """
        data = self.case(['full_name', 'location'], [('Smith, Inc.', 'North'), ('Inc. Smith', 'North')])
        data['extraction']['attempts'] = [dict(result=dict(entities=[
            dict(name='Smith, Inc.', entity_type='organization'),
            dict(name='Inc. Smith', entity_type='organization')]))]
        result = result_progress(data)['goals'][0]['results']
        self.assertEqual((result['identified'], result['identity_pending']), (2, 0))

    def test_timeline_funding_ancestry_and_attribution_are_not_person_counts(self):
        """Different times, relationship directions and attributions identify different results.

        Args:
            self: Table-driven generic research shapes with a duplicate observation.
        Returns:
            None; assert exact result units, preserved distinctions and no fake percentage.
        """
        cases = [(['event', 'date'], [('Opening', '2024'), ('Opening', '2025')]),
            (['funder', 'recipient', 'amount'], [('A', 'B', '10'), ('A', 'B', '20')]),
            (['parent', 'child'], [('A', 'B'), ('B', 'A')]),
            (['actor', 'position', 'attribution'], [('A', 'Proposal X', 'Statement'), ('A', 'Proposal X', 'Allegation')])]
        for fields, rows in cases:
            with self.subTest(fields=fields):
                data = self.case(fields, rows + [rows[0]])
                g = result_progress(data)['goals'][0]
                self.assertEqual(g['results']['supported'], 2)
                self.assertIsNone(g['measurement']['found_percent'])
                self.assertIsNone(target_groups({'goals': [g]})[0]['name_field'])
        data['plan']['goals'][0]['result_shape'] = None
        g = result_progress(data)['goals'][0]
        self.assertIsNone(g['results'])
        self.assertEqual(g['measurement']['kind'], 'evidence_checks')

    def test_requested_target_is_not_replaced_by_estimate_or_clamped(self):
        """A requested sample size is distinct from an estimate and a complete population.

        Args:
            self: Two supported items with a user-requested target of one.
        Returns:
            None; count overflow is explicit and estimates cannot manufacture targets.
        """
        data = self.case(['artifact'], [('A',), ('B',)])
        data['plan']['scope_estimates'] = [dict(lower=2, upper=2, basis='provisional_prior')]
        self.assertIsNone(result_progress(data)['goals'][0]['measurement']['target'])
        data['plan']['completion_criteria'] = [dict(criterion_id='K1', goal_ids=['G1'],
            description='Find one artifact.', kind='enumeration', target_basis='user_request', target_count=1)]
        m = result_progress(data)['goals'][0]['measurement']
        self.assertEqual(m['found_percent'], 200)
        self.assertTrue(m['target_exceeded'])
        data['plan']['completion_criteria'][0]['target_count'] = 0
        g = result_progress(data)['goals'][0]
        self.assertIsNone(g['measurement']['found_percent'])
        from projectclarity.research_results import progress_line
        self.assertIn('Target: 0;', progress_line(g))

    def test_unrepresented_exception_cannot_hide_behind_a_full_count(self):
        """A numerical match does not erase an extra item outside the structured inventory.

        Args:
            self: Synthetic source-supported candidate map with one unmapped exception.
        Returns:
            None; the actual coverage validator must retain a completion blocker.
        """
        from types import SimpleNamespace
        from projectclarity.research_schemas import CompletionCriterion
        session = example_session()
        session.plan.completion_criteria = [CompletionCriterion(criterion_id='K1', goal_ids=['G1'],
            description='Find one result.', kind='enumeration', measurement_method='Inspect evidence.',
            target_basis='user_request', target_count=1, limitations=[])]
        offered = {cid: SimpleNamespace(adjudication_status='supported',
            proposal=SimpleNamespace(goal_ids=['G1'])) for cid in ['C1', 'C2']}
        answer = CoverageAssessment(assessments=[dict(criterion_id='K1', judgment='satisfied',
            result_goal_id='G1', target_count=1, items=[dict(key='Unrepresented item', candidate_ids=['C2'])],
            benchmark_candidate_ids=[], evidence_candidate_ids=['C1'], gaps=[], rationale='Fixture claim.')],
            next_searches=[])
        ledgers = dict(G1=dict(items=[dict(result_id='R1', status='supported', candidate_ids=['C1'])],
            identified=1, supported=1, pending=0, disputed=0, identity_pending=0, count_basis='goal-results-v2'))
        row = validate_assessment(answer, session, offered, result_ledgers=ledgers)[0]
        self.assertEqual(row['status'], 'partial')
        self.assertIn('unrepresented_result_exception', row['blocking_reasons'])

    def test_reference_lists_are_not_rejected_at_twenty_or_forty(self):
        """Large evidence-backed checks must parse without arbitrary tiny reference caps.

        Args:
            self: Schema regression reproducing prior length failures with generic IDs.
        Returns:
            None; unknown references remain the validator's responsibility.
        """
        row = CriterionAssessment(criterion_id='K1', judgment='partial', target_count=None,
            items=[dict(key='one result', candidate_ids=[f'C{i}' for i in range(47)])],
            benchmark_candidate_ids=[], evidence_candidate_ids=[f'C{i}' for i in range(80)],
            gaps=['Not finished'], rationale='Synthetic source references.')
        self.assertEqual(len(row.evidence_candidate_ids), 80)


class GeneralResultProductionTests(unittest.TestCase):
    """Exercise role handling and identity through the real graph and dashboard projection."""

    def test_dual_role_name_variants_agree_in_report_dashboard_and_metrics(self):
        """A generic researcher list must not lose counterevidence or double-count formatting.

        Args:
            self: Test owner; all synthetic evidence is delivered through provider/source transports.
        Returns:
            None; verifies production-generated answer, API projection and recorded metrics.
        """
        f = reporting_fixture.ResultReportingCLITests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.fields, f.unit, f.partial = ['full_name', 'organization'], 'researchers', False
        f.items = [('Dr. Ada Example', 'North'), ('Example, Ada', 'North'), ('Rowan Demo', 'South')]
        original = f.provider

        def provider(request):
            """Add a dual evidence role at the wire boundary, not inside application state.

            Args:
                request: Real SDK HTTP request from the production graph.
            Returns:
                Synthetic response; original extraction/review parsing remains active.
            """
            response = original(request)
            body = json.loads(request.content)
            if body['output_config']['format']['schema']['title'] == 'ExtractionBatch':
                answer = json.loads(response.json()['content'][0]['text'])
                for claim in answer['claims']:
                    claim['research_roles'].append('counterevidence')
                return f.cli.response(text=json.dumps(answer))
            return response

        f.provider = provider
        code, _, err, directory, saved, answer = f.run_case()
        self.assertEqual(code, 0, err)
        from projectclarity.dashboard.projection import snapshot
        view = snapshot(f.cli.output, directory.name)
        result = answer['progress']['goals'][0]['results']
        self.assertEqual((result['identified'], result['supported']), (2, 2))
        self.assertEqual((view['targets'][0]['identified'], view['targets'][0]['supported']), (2, 2))
        self.assertEqual(len(answer['findings']), 3)
        self.assertTrue(all('counterevidence' in r['research_roles'] for r in answer['findings']))
        points = [json.loads(line) for line in (directory / 'dashboard_metrics.jsonl').read_text().splitlines()]
        self.assertEqual(points[-1]['values']['targets']['G1']['supported'], 2)
        self.assertEqual(saved['request']['question'], 'List the source items and their associated values.')

    def test_completion_uses_result_inventory_for_diverse_shapes(self):
        """Stop on complete goal-shaped results without requiring a model to repeat every item.

        Args:
            self: Test owner running isolated researcher, event, funding and ancestry cases.
        Returns:
            None; production graph, report and dashboard must agree on count and readiness.
            Model judgments are synthetic; real-world semantic accuracy is not asserted.
        """
        cases = [(['full_name', 'organization'], [('Ada Example', 'North'), ('Rowan Demo', 'South')], True),
                 (['event', 'date'], [('Opening', '2024-01-01'), ('Closing', '2025-01-01')], False),
                 (['funder', 'recipient'], [('Foundation A', 'Institute B'), ('Foundation C', 'Institute B')], True),
                 (['parent', 'child'], [('Aster', 'Birch'), ('Birch', 'Cedar')], True)]
        for fields, items, relational in cases:
            with self.subTest(fields=fields):
                f = reporting_fixture.ResultReportingCLITests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                f.fields, f.items, f.relational, f.partial = fields, items, relational, False
                f.cli.plan['completion_criteria'] = [dict(criterion_id='K1', goal_ids=['G1'],
                    description='Identify two source-supported results with all requested fields.',
                    kind='enumeration', measurement_method='Check goal-shaped source records.',
                    target_basis='user_request', target_count=2, limitations=[])]
                original, original_cli = f.provider, f.cli.run_cli
                offered = []

                def provider(request):
                    """Judge the complete inventory through the real structured-call boundary.

                    Args:
                        request: Serialized SDK request; other tasks use source-derived fixtures.
                    Returns:
                        Coverage judgment binding to G1, with no model-recounted item list.
                    """
                    wire = json.loads(request.content)
                    if wire['output_config']['format']['schema']['title'] != 'CoverageAssessment':
                        return original(request)
                    payload = json.loads(wire['messages'][0]['content'])
                    for field in ('result_inventory', 'goals', 'criteria', 'candidates', 'context_inventory'):
                        self.assertIn(field, payload)
                    ledger = payload['result_inventory']['G1']
                    self.assertEqual(ledger['required_fields'], fields)
                    self.assertNotIn('items', ledger)
                    self.assertTrue(ledger['manifest_sha256'])
                    self.assertGreater(ledger['identified'], 0)
                    response_schema = wire['output_config']['format']['schema']
                    self.assertIn('result_goal_id', response_schema['$defs']['CriterionAssessment']['properties'])
                    offered.append(payload)
                    return f.cli.response(text=json.dumps(dict(assessments=[dict(
                        criterion_id='K1', judgment='satisfied', items=[], result_goal_id='G1',
                        target_count=2, benchmark_candidate_ids=[],
                        evidence_candidate_ids=[c['candidate_id'] for c in payload['candidates']],
                        gaps=[], rationale='The provided results and quotations satisfy this fixture goal.')], next_searches=[])))

                def run_cli(provider, *flags, **kwargs):
                    """Enable actual coverage and stopping flags, not patched scheduler state.

                    Args:
                        provider: Synthetic transport callable passed by the existing fixture.
                        flags: Production command arguments.
                        kwargs: Fixture terminal/source transport controls.
                    Returns:
                        Actual command result with the same isolated output root.
                    """
                    return original_cli(provider, *flags, '--assess-coverage', '--stop-when-satisfied', **kwargs)

                f.provider, f.cli.run_cli = provider, run_cli
                code, _, err, directory, saved, answer = f.run_case()
                self.assertEqual(code, 0, err)
                self.assertEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
                self.assertEqual(offered[-1]['result_inventory']['G1']['supported'], 2)
                self.assertFalse(offered[-1]['inventory_truncated'])
                m = answer['progress']['goals'][0]['measurement']
                self.assertEqual((m['found_percent'], m['supported_percent']), (100, 100))
                from projectclarity.dashboard.projection import snapshot
                view = snapshot(f.cli.output, directory.name)
                self.assertTrue(view['completion']['ready'])
                self.assertEqual(view['targets'][0]['supported'], 2)

    def test_large_researcher_inventory_reaches_coverage_without_prefix_loss(self):
        """Exercise aggregate context through the production SDK and graph on unseen rows.

        Args:
            self: Test owner using a generated 480-row source, not saved benchmark answers.
        Returns:
            None; proves full application accounting and bounded final transport,
            not live-model reasoning accuracy. This large-completion case allocates
            summary calls explicitly; separate checkpoint tests retain tight budgets.
        """
        from tests.research import test_coverage_checkpoints_cli as checkpoints
        f = checkpoints.CoverageCheckpointCLITests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        _, directory, saved, _ = f.run_case('--max-requests', '220')
        last = saved['coverage']['history'][-1]
        self.assertGreater(last['candidate_total'], 250)
        self.assertFalse(last['inventory_truncated'])
        self.assertEqual(len(last['accounted_candidate_ids']), last['candidate_total'])
        self.assertLessEqual(len(last['offered_candidate_ids']), 24)
        call = json.loads((directory / 'llm_calls' / (last['call_id'] + '.json')).read_text())
        payload = json.loads(call['messages'][1]['content'])
        self.assertEqual(payload['result_inventory']['G1']['identified'], last['candidate_total'])
        self.assertNotIn('items', payload['result_inventory']['G1'])
        self.assertLessEqual(last['input_contract']['total_characters'], 64000)

    def test_failed_final_check_keeps_previous_values_but_cannot_stop(self):
        """Keep progress readable after an invalid assessment without calling stale evidence current.

        Args:
            self: Test owner injecting a malformed final reply at the transport boundary.
        Returns:
            None; verifies retained successful criterion rows and an explicit failed latest check.
        """
        from tests.research import test_coverage_checkpoints_cli as checkpoints
        f = checkpoints.CoverageCheckpointCLITests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original = f.flow.provider

        def provider(request):
            """Fail only the final charged coverage call, preserving all earlier real processing.

            Args:
                request: Actual SDK request produced by the production graph.
            Returns:
                Malformed response at the shared budget ceiling, normal fixture otherwise.
            """
            wire = json.loads(request.content)
            if wire['output_config']['format']['schema']['title'] == 'CoverageAssessment':
                saved = f.flow.cli.saved_session()[1]
                if saved['usage']['requests_attempted'] >= 39:
                    return f.flow.cli.response(text='{')
            return original(request)

        f.flow.provider = provider
        _, _, saved, _ = f.run_case()
        successful = [h for h in saved['coverage']['history'] if h['status'] == 'assessed']
        self.assertTrue(successful)
        self.assertEqual(saved['coverage']['criteria_progress'], successful[-1]['criteria'])
        self.assertEqual(saved['coverage']['history'][-1]['status'], 'not_assessed')
        self.assertNotEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
