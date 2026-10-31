"""Source-bound mapping repair through the public CLI and production graph.

Provider/HTTP replies are synthetic, and the shared scenario disables retrieval
pacing sleeps. The CLI, graph, extraction, review and report accounting execute.
These fixtures establish mechanical delivery and failure accounting for authored
judgments, not a live model's ability to recognize missing contributions. They do
not establish recovery quality on the real MP or researcher investigations.
"""
import copy
import json
import re
import unittest
from unittest.mock import patch
from tests.claim_review.mapping_fixtures import fixture_eligibility

from projectclarity.claim_review.report_mapping import mapping_ledger
from projectclarity.coverage.stopping import completion_status
from projectclarity.research_schemas import ResearchSession
from projectclarity.research_results import result_progress
from projectclarity.provisional_answer import build_provisional_answer, render_provisional_answer
from tests.research.adversarial_source_scenarios import SourceScenario


class ReportMappingTests(unittest.TestCase):
    """Require genuine reviewed repair or an explicit enduring completion blocker."""

    def run_mapping(self, fault=None, budget=30, labels=None, value_field="value", source_values=None):
        """Inject omitted mappings at the transport boundary, never in report code.

        Args:
            self: Test owner retaining temporary production artifacts.
            fault: Optional identity, invalid_goal, invalid_field, missing_assessment,
                empty_repair, unrelated_repair, paraphrase, partial, split, context,
                partial_paraphrase, context_wrong_mapping or crash case. These control authored provider replies or a
                deliberate interrupted call, not the production scheduler.
            budget: Unchanged shared model request allowance.
            labels: Optional literal source names replacing the generic population.
            value_field: Declared report attribute, independent of the generic identity.
            source_values: Optional literal values written into the source beside names.
        Returns:
            Scenario, typed saved run, run directory and actual provider payloads.
            A valid relationship deliberately coexists with absent report records.
        """
        scenario = SourceScenario(self, count=5, arrivals=0, concurrency=1)
        case = scenario.case
        labels = labels or ['Gaudreau', 'Gazan', 'Généreux', 'Genuis', 'Gerretsen']
        case.schema['sections'][0]['fields'][1]['name'] = value_field
        source_values = source_values or [f'Association{i}' for i in range(len(labels))]
        source = '<main><table><tr><th>Item</th><th>Value</th></tr>'
        source += ''.join(f'<tr><td>{name}</td><td>{value}</td></tr>' for name, value in zip(labels, source_values))
        source += '</table></main>'
        case.count = len(labels)
        case.schema['completion_checks'][0]['target_count'] = len(labels)
        case.flow.routes[('example.org', '/research')] = (200, {'Content-Type': 'text/html'}, source.encode())
        original = case.provider
        seen = []
        interrupted = []

        def provider(request):
            """Return controlled omissions and source-supported repair assessments.

            Args:
                request: SDK request containing actual source and candidate inputs.
            Returns:
                Provider-shaped response; unknown tasks delegate to the existing
                source fixture. Every repaired claim still passes production review.
            """
            wire = json.loads(request.content)
            title = wire['output_config']['format']['schema']['title']
            payload = json.loads(wire['messages'][0]['content'])
            seen.append((title, payload))
            if title == 'ExtractionBatch' and payload['source_id'] == case.primary:
                pid = payload['passage']['passage_id']
                child = 'refinement' in payload
                if child and fault == 'crash' and not interrupted:
                    interrupted.append(pid)
                    raise SystemExit(77)
                claims, entities = [], []
                pairs = re.findall(r'Item: ([^|\n]+) \| Value: ([^\n]+)', payload['passage']['text'])
                for i, (name, value) in enumerate(pairs):
                    quote = f'Item: {name} | Value: {value}'
                    entities.extend([dict(entity_id=f'E{i}a', name=name, entity_type='person'),
                                     dict(entity_id=f'E{i}b', name=value, entity_type='organization')])
                    fields = [dict(name='item', value=name), dict(name=value_field, value=value)]
                    record = dict(goal_id='G1', fields=fields)
                    records = [record] if child else []
                    if fault in {'partial', 'split', 'partial_paraphrase'} and not child:
                        records = [dict(goal_id='G1', fields=fields[:1])]
                    if fault in {'context', 'context_wrong_mapping'}:
                        records = []
                    if fault == 'identity':
                        records = [dict(goal_id='G1', fields=fields[1:])]
                    statement = (f'{name} has the listed association {value}.' if child and fault == 'partial_paraphrase'
                        else quote + (' Other assertion.' if child and fault in {'unrelated_repair', 'paraphrase'} else ''))
                    claims.append(dict(claim_id=f'C{i}', statement=statement, goal_ids=['G1'],
                        entity_ids=[f'E{i}a', f'E{i}b'], relationship=dict(subject_id=f'E{i}a', predicate='associated with', object_id=f'E{i}b'),
                        claim_kind='relational', qualifications=[], research_roles=['context'] if fault in {'context', 'context_wrong_mapping'} else ['answer_evidence'],
                        result_records=records, evidence=[dict(passage_id=pid, quote=quote)]))
                    if fault == 'split':
                        separate = copy.deepcopy(claims[-1])
                        separate['claim_id'] += 'Separate'
                        separate['statement'] += ' Separate association fact.'
                        separate['result_records'] = [record] if child else []
                        claims.append(separate)
                if child and fault == 'empty_repair':
                    claims = []
                return case.cli.response(text=json.dumps(dict(entities=entities, claims=claims)))
            if title == 'ClaimReviewBatch':
                assessments = []
                for c in payload['candidates']:
                    claim = c['proposal']
                    maps = []
                    if 'G1' in claim['goal_ids']:
                        values = {f['name'] for r in claim['result_records'] for f in r['fields']}
                        status = 'mapped' if values == {'item', value_field} else 'needs_mapping'
                        if fault == 'split' and not claim['claim_id'].endswith('Separate'):
                            status = 'mapped'
                        if fault == 'identity':
                            status = 'identity_unresolved'
                        if fault == 'context':
                            status = 'not_applicable'
                        maps = [dict(goal_id='WrongGoal' if fault == 'invalid_goal' else 'G1', status=status,
                            field_names=[] if fault == 'context' else ['bad_field'] if fault == 'invalid_field' else ['item'] if fault == 'split' and not claim['claim_id'].endswith('Separate') else ['item', value_field],
                            rationale='Controlled source-bound mapping judgment.')]
                    if fault == 'missing_assessment':
                        maps = []
                    assessments.append(dict(candidate_id=c['candidate_id'], verdict='supported', in_scope=True,
                        rationale='Controlled factual support, separate from mapping.', report_mappings=maps,
                        eligibility_assessments=fixture_eligibility(c, payload),
                        evidence_quotes=[claim['evidence'][0]['quote']]))
                return case.cli.response(text=json.dumps(dict(assessments=assessments)))
            return original(request)

        case.flow.provider = provider
        code, directory, saved, _ = scenario.run(budget=budget, search=False)
        self.assertIn(code, (0, 1, 77) if fault == 'crash' else (0, 1))
        return scenario, ResearchSession.model_validate(saved), directory, seen

    def test_omitted_mp_records_with_valid_relationships_reach_report(self):
        """Verify all five source names survive the real repair and review route.

        Args:
            self: Test owner with production-path fixture.
        Returns:
            None; fails on silent loss, missing re-review or duplicate repair calls.
        """
        _, session, directory, seen = self.run_mapping()
        progress = result_progress(session.model_dump(mode='json'))
        inventory = progress['goals'][0]['results']
        self.assertEqual(inventory['supported'], 5)
        self.assertEqual(progress['mapping_ledger']['unresolved'], 0)
        resolved = [r for r in progress['mapping_ledger']['rows'] if r['status'] == 'resolved']
        self.assertEqual(len(resolved), 5)
        attempts = session.extraction['attempts']
        self.assertEqual(sum(bool(a['refinement_parent']) for a in attempts), 1)
        self.assertTrue(all(r['resolved_by'] for r in resolved))
        self.assertTrue(any(p.get('refinement', {}).get('mapping_obligations') for t, p in seen if t == 'ExtractionBatch'))
        self.assertTrue(completion_status(session, directory.parent)['ready'])

    def test_partial_identity_record_receives_separate_supported_field(self):
        """Merge repaired association fields using declared identity, not prose joins.

        Args:
            self: Fixture owner for a missing party/association field scenario.
        Returns:
            None; the complete record has the original identity and repaired field.
        """
        _, session, _, _ = self.run_mapping(fault='split')
        progress = result_progress(session.model_dump(mode='json'))
        self.assertEqual(progress['goals'][0]['results']['supported'], 5)
        self.assertEqual(progress['mapping_ledger']['unresolved'], 0)

    def test_researcher_location_contribution_uses_same_generic_route(self):
        """Prove the mechanism is not conditional on parliamentary names or fields.

        Args:
            self: Fixture owner using a distinct synthetic researcher population.
        Returns:
            None; source-backed location/affiliation association reaches the report.
        """
        _, session, _, _ = self.run_mapping(labels=['Razavi-Far', 'Researcher-B'],
            value_field='canada_basis', source_values=['UNB, Fredericton, Canada', 'Toronto, Canada'])
        self.assertEqual(result_progress(session.model_dump(mode='json'))['goals'][0]['results']['supported'], 2)
        rows = result_progress(session.model_dump(mode='json'))['goals'][0]['results']['records']
        self.assertEqual(next(r for r in rows if r['values']['item'] == 'Razavi-Far')['values']['canada_basis'],
                         'UNB, Fredericton, Canada')
        self.assertEqual(mapping_ledger(session.model_dump(mode='json'))['unresolved'], 0)

    def test_context_and_population_facts_create_no_report_rows(self):
        """Respect independent not-applicable judgments without forced records.

        Args:
            self: Test owner supplying context-role assertions.
        Returns:
            None; neither rows nor mapping repairs are manufactured for context.
        """
        _, session, _, _ = self.run_mapping(fault='context')
        progress = result_progress(session.model_dump(mode='json'))
        self.assertEqual(progress['goals'][0]['results']['identified'], 0)
        self.assertEqual(progress['mapping_ledger']['unresolved'], 0)
        self.assertFalse(session.workflow['report_mapping_repairs'])

    def test_identity_uncertainty_is_visible_and_never_joined(self):
        """Preserve incomplete observations without inferring missing identity.

        Args:
            self: Test owner with source-backed values but ambiguous mappings.
        Returns:
            None; gaps block completion and appear in the saved report projection.
        """
        _, session, directory, _ = self.run_mapping(fault='identity')
        status = completion_status(session, directory.parent)
        self.assertFalse(status['ready'])
        self.assertGreaterEqual(status['mapping_ledger']['unresolved'], 5)
        self.assertEqual({r['status'] for r in status['mapping_ledger']['rows']}, {'identity_unresolved'})
        answer = build_provisional_answer(session, directory.parent)
        self.assertEqual(answer['progress']['mapping_ledger'], status['mapping_ledger'])
        from projectclarity.dashboard.projection import snapshot
        from projectclarity.coverage.result_inventory import coverage_results
        from projectclarity.nodes.assess_research_gaps.node import checked_candidates
        view = snapshot(directory.parent, session.session_id)
        self.assertEqual(view['result_progress']['mapping_ledger'], status['mapping_ledger'])
        _, valid = checked_candidates(session, directory.parent)
        self.assertEqual(coverage_results(session, valid)['G1']['mapping_gaps'],
                         [r for r in status['mapping_ledger']['rows'] if r['blocking']])
        rendered = render_provisional_answer(answer)
        self.assertIn('Unresolved claim-to-report mappings', rendered)
        self.assertLess(rendered.index('## ' + answer['answer_schema']['title']),
                        rendered.index('## Unresolved claim-to-report mappings'))
        self.assertFalse(session.workflow['report_mapping_repairs'])

    def test_invalid_references_preserve_support_but_fail_closed(self):
        """Reject unknown fields/goals and missing coverage independently of truth.

        Args:
            self: Test owner iterating malformed-but-parseable reviewer judgments.
        Returns:
            None; supported originals survive while completion remains blocked.
        """
        for fault in ('invalid_goal', 'invalid_field', 'missing_assessment'):
            with self.subTest(fault=fault):
                _, session, directory, _ = self.run_mapping(fault=fault)
                status = completion_status(session, directory.parent)
                self.assertFalse(status['ready'])
                self.assertEqual(status['mapping_ledger']['unresolved'], 5)
                self.assertEqual({r['status'] for r in status['mapping_ledger']['rows']}, {'unknown'})
                self.assertTrue(all(c['adjudication_status'] == 'supported' for c in session.extraction['candidates']))

    def test_completed_empty_or_unrelated_repairs_never_clear_gaps(self):
        """Reject completion-by-retry and completion-by-unrelated-success.

        Args:
            self: Test owner checking empty and semantically unlinked child replies.
        Returns:
            None; exhausted gaps remain explicit even if other rows are supported.
        """
        for fault in ('empty_repair', 'unrelated_repair'):
            with self.subTest(fault=fault):
                _, session, directory, _ = self.run_mapping(fault=fault)
                ledger = mapping_ledger(session.model_dump(mode='json'))
                self.assertEqual(ledger['unresolved'], 5)
                self.assertEqual(sum(r['status'] == 'exhausted' for r in ledger['rows']), 5)
                self.assertFalse(completion_status(session, directory.parent)['ready'])

    def test_restart_preserves_ownership_and_does_not_replay_repairs(self):
        """Reconcile a completed run repeatedly using the actual adjudication node.

        Args:
            self: Fixture owner retaining saved source integrity and repair receipts.
        Returns:
            None; serialized reload and repeated adjudication create no extra attempts.
        """
        from projectclarity.nodes.adjudicate_claims.node import adjudicate_claims
        _, session, directory, _ = self.run_mapping(fault='empty_repair')
        before = copy.deepcopy(session.extraction['attempts'])
        for _ in range(2):
            session = ResearchSession.model_validate_json(session.model_dump_json())
            adjudicate_claims({'session': session}, directory.parent)
        self.assertEqual(session.extraction['attempts'], before)
        self.assertEqual(mapping_ledger(session.model_dump(mode='json'))['unresolved'], 5)

    def test_budget_stop_keeps_mapping_gap_and_original_facts(self):
        """A low unchanged budget cannot erase obligations or claim success.

        Args:
            self: Fixture owner exercising the actual shared request ceiling.
        Returns:
            None; any supported unmapped originals remain blocking and inspectable.
        """
        _, session, directory, _ = self.run_mapping(budget=7)
        ledger = mapping_ledger(session.model_dump(mode='json'))
        self.assertGreater(ledger['unresolved'], 0)
        self.assertFalse(completion_status(session, directory.parent)['ready'])
        self.assertLessEqual(session.usage.requests_attempted, 7)

    def test_uncertain_paid_repair_survives_real_cli_resume_without_replay(self):
        """Interrupt after reservation and restart via the production resume command.

        Args:
            self: Test owner preserving actual checkpoint and model-call accounting.
        Returns:
            None; no uncertain repair is replayed, budgets persist and its gap blocks.
        """
        scenario, before, directory, seen = self.run_mapping(fault='crash')
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, saved = scenario.case.flow.resume(before.session_id)
        after = ResearchSession.model_validate(saved)
        repairs = [p for t, p in seen if t == 'ExtractionBatch' and 'refinement' in p]
        self.assertEqual(len(repairs), 1)
        self.assertGreaterEqual(after.usage.requests_attempted, before.usage.requests_attempted)
        self.assertLessEqual(after.usage.requests_attempted, 30)
        self.assertEqual(after.workflow['report_mapping_repairs'], before.workflow['report_mapping_repairs'])
        self.assertEqual(mapping_ledger(saved)['unresolved'], 5)
        self.assertFalse(completion_status(after, directory.parent)['ready'])

    def test_legacy_unknown_and_false_mapped_judgments_cannot_prove_completion(self):
        """Reject absent old assessments and mapped claims without supplied fields.

        Args:
            self: Test owner mutating copies of a fully source-verified fixture run.
        Returns:
            None; neither legacy loading nor a reviewer label creates report fields.
        """
        _, session, _, _ = self.run_mapping(fault='invalid_field')
        original = session.model_dump(mode='json')
        for fault in ('legacy', 'false_mapped', 'duplicate_goal'):
            data = copy.deepcopy(original)
            for receipt in data['claim_reviews'].values():
                assessment = receipt['assessment']
                if fault == 'legacy':
                    assessment.pop('report_mappings', None)
                else:
                    assessment['report_mappings'] = [dict(goal_id='G1', status='mapped',
                        field_names=['item', 'value'], rationale='Adversarial incomplete mapping.')]
                    if fault == 'duplicate_goal':
                        assessment['report_mappings'] *= 2
            ledger = mapping_ledger(data)
            self.assertEqual(ledger['unresolved'], 5)
            self.assertEqual({r['status'] for r in ledger['rows']}, {'unknown'})
