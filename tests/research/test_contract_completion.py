"""Prove answer-owned stopping through production paths with synthetic transports.

Tests retain the real graph, SDK serialization, parsing, source validation,
scheduler, budgets and saved artifacts. Replies are controlled evidence judgments,
not live-model accuracy measurements. Large-source coverage uses the production
128,000-character intermediate and 64,000-character final envelope budgets.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings, fixture_eligibility
import copy
import json
import unittest
from collections import Counter
from tests.retrieval.retrieval_test_fixtures import public_dns

from projectclarity.answer_schema.binding import bind_report
from projectclarity.answer_schema.contracts import AnswerSchema
from projectclarity.coverage.readiness import readiness_criteria
from projectclarity.coverage.stopping import completion_status, evidence_revision
from projectclarity.research_schemas import ResearchSession
from tests.research.research_test_fixtures import example_session
from tests.research.test_answer_schema import specification
from tests.research import test_result_reporting_cli as reporting


def explicit_specification(count=2, format='records', supporting=False):
    """Define a generic fixture answer contract with no domain-specific counting rule.

    Args:
        count: Explicit requested record count; None makes the main check nonnumeric.
        format: Report presentation kind, including narrative and comparison.
        supporting: Whether to add a separate supporting check with its own unit.
    Returns:
        Schema-valid JSON specification for design-provider fixtures. It contains
        requirements only, not source findings or a guessed research denominator.
    """
    schema = specification((), (), format=format) if format == 'narrative' else specification(
        ('item', 'value'), ('text', 'text'), identity=('item',), format=format)
    schema['completion_checks'] = [dict(criterion_id='A1', goal_ids=['G1'],
        description='Establish the requested source records and their values.',
        kind='enumeration' if count is not None else 'evidence_sufficiency',
        measurement_method='Check evidence and the requested report requirements.',
        target_basis='user_request' if count is not None else 'not_applicable',
        target_count=count, limitations=[], unit=schema['sections'][0]['unit'],
        result_goal_id='G1' if count is not None else None,
        evidence_required=['Support each requested value and the requested scope.'])]
    if supporting:
        schema['completion_checks'].append(dict(criterion_id='A2', goal_ids=['G1'],
            description='Establish the scope of the source collection.', kind='constraints',
            measurement_method='Use the actual source context and source evidence.',
            target_basis='not_applicable', target_count=None, limitations=[],
            unit='document scope', result_goal_id=None,
            evidence_required=['Establish which collection these records describe.']))
    return schema


class ContractCompletionTests(unittest.TestCase):
    """Separate positive mechanics, adversarial gates and model-quality limitations."""

    def run_contract(self, *, count=2, format='records', large=False, fault=None, budget=220,
                     repeated_scope=False, digest_transform=None):
        """Run a designed report through the actual terminal entry point.

        Args:
            self: Test owner registering transport fixtures and cleanup.
            count: Number of deterministic unseen fixture records to put in the source.
            format: Report kind; comparisons/narratives use nonnumeric completion.
            large: Include long complete quotations and qualifications so final
                evidence really exceeds the production aggregate input allowance.
            fault: Optional wrong_unit, missing_field, conflict, unknown_target,
                digest_invalid, digest_crash, digest_correction_crash, coverage_crash, provider_failure,
                row_reference, or satisfied_gaps fault
                at the provider boundary.
                budget accepts an ordinary incomplete outcome under a lower allowance.
            budget: Explicit shared request allowance; never changed by production code.
            repeated_scope: Use long but valid scope/question text repeated by actual
                qualification receipts, exercising transport normalization rather
                than requiring longer source quotations or live model judgments.
            digest_transform: Optional synthetic reply callback(payload, answer, fixture)
                at the digest SDK boundary; production code remains unchanged.
        Returns:
            Fixture, typed saved session, run directory and captured SDK payloads.
            Network/model transports alone are synthetic; all production nodes run.
        """
        fixture = reporting.ResultReportingCLITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fields = ['item', 'value']
        fixture.items = [(f'Event_{(i*17+23)%997}', f'Value_{i*13+7}') for i in range(count)]
        fixture.partial = False
        fixture.relational = False
        numeric = None if format in {'comparison','narrative','timeline'} else count
        schema = explicit_specification(numeric, format, supporting=True)
        if repeated_scope:
            schema['scope'] = ('This fixture concerns the listed source events and their recorded values. ' * 27)[:1900]
            schema['sections'][0]['question'] = ('Which events and values are present in the supplied event register? ' * 27)[:1500]
        fixture.cli.plan['goals'][1]['requirement_ids'] = ['A2']
        if fault == 'unknown_target':
            schema['completion_checks'][0].update(target_basis='research', target_count=None)
        quotes = {}
        for i, item in enumerate(fixture.items):
            statement = fixture.statement(item)
            quotes[item[0]] = ([statement + ' ' + (
                f'Source observation {i}, context {j}, retained qualification and chronological detail. ' * 50)[:2670]
                for j in range(4)] if large else [statement])
        source = '<html><head><title>Fixture event register</title></head><body><main><table><tr><th>Record</th></tr>'
        source += ''.join('<tr><td>' + ' '.join(quotes[item[0]]) + '</td></tr>' for item in fixture.items)
        source += '</table></main></body></html>'
        seen, crashes = [], []
        original_run = fixture.cli.run_cli

        def provider(request):
            """Return source-derived fixtures at the real structured-output HTTP boundary.

            Args:
                request: Serialized request produced by the SDK and production node.
            Returns:
                Synthetic provider response or one deliberate abrupt digest interruption.
                Captures actual inputs; never replaces graph nodes or evidence validators.
            Review replies include authored report-mapping judgments; these do not
            establish live semantic recognition accuracy.
            """
            wire = json.loads(request.content)
            title = wire['output_config']['format']['schema']['title']
            payload = json.loads(wire['messages'][0]['content'])
            seen.append((title, payload))
            if title == 'AnswerSchema':
                answer = schema
            elif title == 'ResearchPlan':
                return fixture.cli.response()
            elif title == 'ExtractionBatch':
                claims = []
                for i, item in enumerate(fixture.items):
                    evidence = quotes[item[0]]
                    if not all(q in payload['passage']['text'] for q in evidence):
                        continue
                    fields = [dict(name=k, value=v) for k,v in zip(fixture.fields,item)]
                    if fault == 'missing_field' and i == 0:
                        fields.pop()
                    records = [] if format == 'narrative' else [dict(goal_id='G1', fields=fields)]
                    if fault == 'conflict' and i == 0:
                        records.append(dict(goal_id='G1', fields=[fields[0],dict(name='value',value='Conflicting value')]))
                    claims.append(dict(claim_id=f'C{i}', statement=fixture.statement(item),
                        goal_ids=['G1'], entity_ids=[], relationship=None, claim_kind='non_relational',
                        qualifications=[evidence[j%4][:900] for j in range(6)] if large else [],
                        research_roles=['answer_evidence'], result_records=records,
                        evidence=[dict(passage_id=payload['passage']['passage_id'],quote=q) for q in evidence]))
                answer = dict(entities=[],claims=claims)
            elif title == 'ClaimReviewBatch':
                answer = dict(assessments=[dict(candidate_id=c['candidate_id'], verdict='supported',
                    in_scope=True, rationale='Controlled fixture: source contains the assigned values.',
                    report_mappings=fixture_mappings(c, payload), eligibility_assessments=fixture_eligibility(c, payload), evidence_quotes=[c['proposal']['evidence'][0]['quote']]) for c in payload['candidates']])
            elif title == 'BoundedDigest':
                if fault == 'digest_crash' and not crashes:
                    crashes.append(payload['inputs'][0]['input_id'])
                    raise SystemExit(77)
                pool = [c['candidate_id'] for c in payload['inputs']] if payload['stage']=='evidence' else [
                    cid for c in payload['inputs'] for cid in c['witness_candidate_ids']]
                answer = dict(covered_input_ids=[c['input_id'] for c in payload['inputs']],
                    summary='Controlled fixture transport summary; not live semantic evidence.',
                    witness_candidate_ids=list(dict.fromkeys(pool))[:24], limitations=[],
                    unresolved_conflict=fault == 'summary_conflict', unresolved_identity=False, requires_detail=False)
                if fault == 'digest_invalid':
                    answer['covered_input_ids']=['invented_candidate']
                if digest_transform:
                    answer = digest_transform(payload, answer, fixture)
            elif title == 'CoverageAssessment':
                if fault == 'coverage_crash':
                    raise SystemExit(77)
                if fault == 'provider_failure':
                    import httpx2
                    return httpx2.Response(400, json={'type':'error', 'error':{
                        'type':'invalid_request_error', 'message':'Coverage diagnostic provider failure fixture.'}})
                registry = payload.get('evidence_registry', payload['candidates'])
                ids = [c['candidate_id'] for c in registry]
                represented = payload['result_inventory'].get('G1', {}).get('identified', len(ids))
                satisfied = represented >= count
                assessments = []
                for check in schema['completion_checks']:
                    assessments.append(dict(criterion_id=check['criterion_id'],
                        judgment='satisfied' if satisfied else 'partial', items=[],
                        target_count=check['target_count'], result_goal_id=check['result_goal_id'],
                        benchmark_candidate_ids=[], evidence_candidate_ids=ids,
                        gaps=[] if satisfied else ['More requested source entries remain.'],
                        rationale='Controlled whole-answer judgment across all supplied evidence.'))
                if fault == 'wrong_unit':
                    assessments[-1]['result_goal_id']='G1'
                if fault == 'satisfied_gaps':
                    assessments[-1]['gaps'] = ['Source attribution remains unresolved.']
                if fault == 'row_reference':
                    for assessment in assessments:
                        assessment['evidence_candidate_ids'] = ['unoffered_report_row']
                answer = dict(assessments=assessments,next_searches=[],scope_estimates=[])
            else:
                raise AssertionError(title)
            return fixture.cli.response(text=json.dumps(answer))

        def run(handler, *args, **kwargs):
            """Install only the fixture page and real terminal options.

            Args:
                handler: Synthetic provider handler passed by the existing harness.
                args: Real extraction/review flags from the reporting fixture.
                kwargs: Harness settings retaining production DNS/acquisition behavior.
            Returns:
                Ordinary CLI result including deliberately interrupted exit codes.
            """
            fixture.flow.routes[('example.org','/research')] = (200,{'Content-Type':'text/html'},source.encode())
            return original_run(handler,*args,'--design-answer','--assess-coverage',
                '--stop-when-satisfied','--max-output-tokens','8192',**kwargs)

        fixture.provider = provider
        fixture.flow.provider = provider
        fixture.cli.run_cli = run
        if fault in {'digest_crash', 'coverage_crash', 'digest_correction_crash'}:
            code, _, _ = fixture.cli.run_cli(provider, '--extract', '--review-claims',
                '--max-requests', str(budget), '--question', 'List the source items and their associated values.',
                plan_only=False, details=False, source_dns=public_dns)
            self.assertEqual(code, 77)
        else:
            code, _, err, directory, saved, _ = fixture.run_case(budget=budget)
            if fault is None:
                self.assertEqual(code, 0, err)
            else:
                self.assertIn(code, (0, 1), err)
            return fixture, ResearchSession.model_validate(saved), directory, seen
        directory, saved = fixture.cli.saved_session()
        return fixture, ResearchSession.model_validate(saved), directory, seen

    def test_contract_owns_checks_and_supporting_units_are_not_report_rows(self):
        """Reject unit mismatches and preserve explicit contract authority over studies.

        Args:
            self: Test owner validating requirements without external calls.
        Returns:
            None; malformed bindings fail, planner checks are replaced, and study
            ambiguities remain advisory only for explicit new contracts.
        """
        session = example_session()
        schema = explicit_specification(supporting=True)
        bad = copy.deepcopy(schema)
        bad['completion_checks'][0]['unit'] = 'a different counting unit'
        with self.assertRaises(ValueError):
            AnswerSchema.model_validate(bad)
        bad = copy.deepcopy(schema)
        bad['completion_checks'][1]['result_goal_id']='G1'
        with self.assertRaises(ValueError):
            AnswerSchema.model_validate(bad)
        session.answer_schema = schema
        bind_report(session.plan,schema)
        self.assertEqual([c.criterion_id for c in session.plan.completion_criteria],['A1','A2'])
        session.coverage_study = dict(status='partial',rejected_checks=[{'check_index':1}],
            readiness=dict(coverage_checks=[],unresolved_questions=['Try another particular website.']))
        self.assertEqual(readiness_criteria(session),[])
        before = evidence_revision(session)
        session.coverage_study['readiness']['unresolved_questions'].append('Another exploratory lead.')
        self.assertEqual(before,evidence_revision(session))

    def test_exact_row_citations_resolve_through_production_provenance(self):
        """Reject invented report-row references that were not offered through the CLI.

        Args:
            self: Test owner providing row IDs at the provider boundary.
        Returns:
            None. The bounded input no longer offers report-row IDs. Invented
            references are rejected and the original response remains inspectable.
            No semantic equivalence of different people or events is inferred.
        """
        fixture, session, directory, seen = self.run_contract(fault='row_reference')
        self.assertFalse(completion_status(session, fixture.cli.output)['ready'])
        last = session.coverage['history'][-1]
        self.assertEqual(last['error_code'], 'unknown_or_changed_evidence')
        payload = next(p for title, p in reversed(seen) if title == 'CoverageAssessment')
        self.assertNotIn('items', payload['result_inventory']['G1'])

    def test_comparison_and_narrative_stop_without_a_population_count(self):
        """Exercise nonnumeric contracts through the public terminal workflow.

        Args:
            self: Test owner running three report shapes with controlled judgments.
        Returns:
            None; checks production stopping, separate supporting units, source
            context delivery and the dashboard's identical completion projection.
        """
        for format in ('comparison','narrative','timeline'):
            with self.subTest(format=format):
                fixture,session,directory,seen = self.run_contract(format=format)
                self.assertEqual(session.workflow['stop_reason'],'criteria_satisfied')
                self.assertTrue(completion_status(session,fixture.cli.output)['ready'])
                self.assertTrue(all(c['target_count'] is None for c in session.coverage['criteria_progress']))
                diagnostic = session.coverage['history'][-1]['diagnostics']
                self.assertTrue(all(c['population']['state'] == 'not_applicable'
                    for c in diagnostic['criteria']))
                self.assertEqual(diagnostic['declared_criteria'], session.answer_schema['completion_checks'])
                self.assertEqual(diagnostic['section_requirements'][0]['format'], format)
                from projectclarity.dashboard.projection import snapshot
                view=snapshot(fixture.cli.output,directory.name)
                self.assertTrue(view['completion']['contract_owned'])
                self.assertEqual(view['completion']['total'],2)
                payload=next(p for title,p in seen if title=='CoverageAssessment')
                self.assertIn('sources',payload)

    def test_adversarial_global_satisfaction_cannot_bypass_contract_gates(self):
        """Keep missing fields, conflicting values, wrong units and unknown totals blocked.

        Args:
            self: Test owner injecting optimistic global replies through real SDK transport.
        Returns:
            None; each saved production outcome remains incomplete despite those replies.
        """
        for fault in ('wrong_unit','missing_field','conflict','unknown_target'):
            with self.subTest(fault=fault):
                fixture,session,_,_=self.run_contract(fault=fault,budget=14)
                self.assertNotEqual(session.workflow['stop_reason'],'criteria_satisfied')
                self.assertFalse(completion_status(session,fixture.cli.output)['ready'])
                if fault in {'missing_field', 'conflict'}:
                    supporting=next(c for c in session.coverage['criteria_progress'] if c['criterion_id']=='A2')
                    self.assertEqual(supporting['status'],'model_assessed_satisfied')

    def test_repeated_qualification_scope_fits_through_real_cli(self):
        """Bound large repeated qualification data at the production HTTP boundary.

        Args:
            self: Test owner invoking the real CLI, scheduler and review pipeline.
        Returns:
            None. Every record remains stored, no full inventory reaches the model,
            and existing completion gates consume authored responses.
            This proves delivery/structural accounting, not live semantic accuracy.
        """
        fixture, session, directory, seen = self.run_contract(count=140, repeated_scope=True)
        self.assertEqual(session.workflow['stop_reason'], 'criteria_satisfied')
        last = session.coverage['history'][-1]
        self.assertEqual(last['input_mode'], 'bounded_tree')
        self.assertEqual(last['diagnostics']['validation_status'], 'accepted')
        final = next(p for title, p in reversed(seen) if title == 'CoverageAssessment')
        self.assertNotIn('items', final['result_inventory']['G1'])
        self.assertEqual(final['result_inventory']['G1']['identified'], 140)
        self.assertEqual(len(final['evidence_batches']), 1)
        self.assertLessEqual(len(final['evidence_registry']), 24)
        for receipt in (directory / 'input_receipts').glob('*.json'):
            value = json.loads(receipt.read_text())
            self.assertFalse(value['errors'])
            self.assertLessEqual(value['total_characters'], value['limit_characters'])
        self.assertTrue(completion_status(session, fixture.cli.output)['ready'])

    def test_diagnostics_survive_rejected_and_downgraded_cli_answers(self):
        """Trace validation failures and optimistic-with-gaps replies through the CLI.

        Args:
            self: Owner of scripted provider responses; all production validators run.
        Returns:
            None. Saved diagnostics identify the call and rejection or downgrade;
            completion stays blocked. No live semantic accuracy is asserted.
        """
        for fault in ('wrong_unit', 'satisfied_gaps'):
            with self.subTest(fault=fault):
                fixture, session, directory, _ = self.run_contract(fault=fault, budget=14)
                saved = json.loads((directory / 'coverage.json').read_text())
                row = saved['history'][-1]
                diagnostic = row['diagnostics']
                self.assertEqual(diagnostic['stage'], 'finished')
                self.assertEqual(diagnostic['provider_receipt']['call_id'], row['call_id'])
                trace = directory / diagnostic['provider_receipt']['trace_path']
                self.assertTrue(trace.is_file())
                if fault == 'wrong_unit':
                    self.assertEqual(diagnostic['validation_status'], 'rejected')
                    self.assertEqual(diagnostic['error_code'], 'answer_contract_result_unit_mismatch')
                else:
                    self.assertEqual(diagnostic['validation_status'], 'accepted')
                    self.assertIn('satisfied_proposal_has_gaps', diagnostic['warnings'])
                    self.assertIn('unresolved_gaps', diagnostic['criteria'][-1]['blocking_reasons'])
                    self.assertEqual(diagnostic['criteria'][-1]['accepted_status'], 'partial')
                self.assertFalse(completion_status(session, fixture.cli.output)['ready'])

    def test_diagnostics_are_persisted_before_interrupted_coverage_dispatch(self):
        """Leave the input snapshot and call link on disk when transport is interrupted.

        Args:
            self: CLI fixture owner injecting SystemExit at the real SDK HTTP boundary.
        Returns:
            None. The saved stage says dispatch_pending, never successful validation;
            the separately persisted attempt ledger records the uncertain call.
        """
        _, session, directory, _ = self.run_contract(fault='coverage_crash')
        row = json.loads((directory / 'coverage.json').read_text())['history'][-1]
        self.assertEqual(row['diagnostics']['stage'], 'dispatch_pending')
        self.assertEqual(row['diagnostics']['validation_status'], 'not_run')
        call_id = row['diagnostics']['call_id']
        attempts = [a for a in session.model_attempts if a['call_id'] == call_id]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['status'], 'uncertain')
        self.assertTrue((directory / 'llm_calls' / (call_id + '.json')).is_file())

    def test_provider_failure_retains_diagnostics_and_full_error_receipt(self):
        """Join a real SDK error path to coverage history without losing provider text.

        Args:
            self: CLI fixture owner returning an HTTP error only for coverage requests.
        Returns:
            None. No judgment is accepted, usage remains unknown, and the linked
            private trace/attempt records retain the complete synthetic error message.
        """
        _, session, directory, _ = self.run_contract(fault='provider_failure', budget=14)
        row = session.coverage['history'][-1]
        diagnostic = row['diagnostics']
        self.assertEqual(diagnostic['validation_status'], 'not_run')
        self.assertIsNotNone(diagnostic['error_code'])
        self.assertIsNone(diagnostic['provider_receipt']['input_tokens'])
        trace = json.loads((directory / diagnostic['provider_receipt']['trace_path']).read_text())
        self.assertEqual(trace['call_id'], row['call_id'])
        attempt = next(a for a in session.model_attempts if a['call_id'] == row['call_id'])
        receipt = directory / attempt['diagnostics']['provider_error_path']
        self.assertIn('Coverage diagnostic provider failure fixture.', receipt.read_text())

    def test_large_production_population_finishes_via_complete_reusable_batches(self):
        """Exceed the real aggregate input allowance and stop through production orchestration.

        Args:
            self: Test owner generating 36 unseen records with long, intact evidence.
        Returns:
            None; proves every final record reaches a current digest, global judgment,
            dashboard projection and semantic stopping gate. Mutations invalidate reuse.
            Scripted judgments do not prove live-model compression fidelity or accuracy.
        """
        fixture,session,directory,seen=self.run_contract(count=36,large=True)
        self.assertEqual(session.workflow['stop_reason'],'criteria_satisfied')
        last=session.coverage['history'][-1]
        self.assertEqual(last['input_mode'],'bounded_tree')
        from projectclarity.coverage.evidence_batches import fingerprint
        digests=Counter(fingerprint(p) for t,p in seen if t=='BoundedDigest')
        self.assertTrue(digests)
        self.assertEqual(max(digests.values()),1)
        batch=session.coverage['batch_progress']
        self.assertGreater(batch['total'],1)
        self.assertEqual(batch['completed'],batch['total'])
        self.assertEqual(batch['candidates_reviewed'],len(session.extraction['candidates']))
        final=next(p for title,p in reversed(seen) if title=='CoverageAssessment')
        self.assertEqual(final['candidates'],[])
        self.assertEqual(final['context_inventory']['offered'], 0)
        self.assertEqual(final['context_inventory']['summarized'], len(session.extraction['candidates']))
        self.assertEqual(final['context_inventory']['witness_references'], len(final['evidence_registry']))
        self.assertEqual(len(final['evidence_batches']),1)
        represented={cid for bid in batch['batch_ids'] for cid in session.coverage['evidence_batches'][bid]['covered_candidate_ids']}
        self.assertEqual(represented,{c['candidate_id'] for c in session.extraction['candidates']})
        self.assertNotIn('items', final['result_inventory']['G1'])
        self.assertEqual(final['result_inventory']['G1']['identified'],36)
        self.assertLessEqual(len(final['evidence_registry']),24)
        self.assertTrue(completion_status(session,fixture.cli.output)['ready'])
        from projectclarity.dashboard.projection import snapshot
        self.assertEqual(snapshot(fixture.cli.output,directory.name)['completion']['batch_progress']['completed'],batch['total'])
        changed=session.model_copy(deep=True)
        changed.coverage['evidence_batches'][batch['batch_ids'][0]]['status']='failed'
        self.assertFalse(completion_status(changed,fixture.cli.output)['ready'])
        changed=session.model_copy(deep=True)
        changed.coverage['evidence_batches'][batch['batch_ids'][0]]['assessment']['summary']='Altered digest'
        self.assertFalse(completion_status(changed,fixture.cli.output)['ready'])
        changed=session.model_copy(deep=True)
        changed.coverage['history'][-1].pop('bounded_manifest')
        self.assertFalse(completion_status(changed,fixture.cli.output)['ready'])
        changed=session.model_copy(deep=True)
        changed.answer_schema['completion_checks'][0]['evidence_required'].append('A newly required source comparison.')
        self.assertFalse(completion_status(changed,fixture.cli.output)['ready'])
        changed=session.model_copy(deep=True)
        changed.extraction['candidates'][0]['adjudication_status']='pending'
        self.assertFalse(completion_status(changed,fixture.cli.output)['ready'])
        path=directory/session.documents[0].text_path
        path.write_text('Changed after the final assessment.')
        self.assertFalse(completion_status(session,fixture.cli.output)['ready'])

    def test_invalid_digests_have_bounded_retries_and_cannot_approve_completion(self):
        """Exercise incorrect digest references above the real production context limit.

        Args:
            self: Test owner injecting invalid model output, not bypassing its validator.
        Returns:
            None; every rejected batch retains its receipts, consumes the ordinary
            allowance and receives at most one explicit correction. Completion stays blocked.
        """
        fixture,session,_,seen=self.run_contract(count=36,large=True,fault='digest_invalid')
        receipts=session.coverage['evidence_batches']
        self.assertTrue(receipts)
        self.assertTrue(all(r['status']=='failed' and r['attempts']<=2 for r in receipts.values()))
        self.assertTrue(all(r['attempts']==2 for r in receipts.values()))
        self.assertFalse(completion_status(session,fixture.cli.output)['ready'])
        self.assertLessEqual(session.usage.requests_attempted,session.request.budget.max_requests)
        self.assertTrue(any(t=='BoundedDigest' for t,_ in seen))

    def test_interrupted_digest_is_not_replayed_and_resume_retains_budget(self):
        """Restart the actual terminal workflow after an ambiguous provider interruption.

        Args:
            self: Test owner interrupting the first digest transport after reservation.
        Returns:
            None; production resume preserves the consumed request and completed
            evidence, marks the uncertain digest, and cannot approve the incomplete set.
        """
        fixture,before,_,seen=self.run_contract(count=36,large=True,fault='digest_crash')
        reserved=[r for r in before.coverage['evidence_batches'].values() if r['status']=='reserved']
        self.assertEqual(len(reserved),1)
        first=next(p for t,p in seen if t=='BoundedDigest')
        first_ids=[c['input_id'] for c in first['inputs']]
        _,saved=fixture.flow.resume(before.session_id)
        session=ResearchSession.model_validate(saved)
        receipt=session.coverage['evidence_batches'][reserved[0]['batch_id']]
        self.assertEqual(receipt['status'],'uncertain')
        self.assertEqual(receipt['attempts'],1)
        self.assertEqual(sum([c['input_id'] for c in p['inputs']]==first_ids
            for t,p in seen if t=='BoundedDigest'),1)
        self.assertGreaterEqual(session.usage.requests_attempted,before.usage.requests_attempted)
        self.assertLessEqual(session.usage.requests_attempted,session.request.budget.max_requests)
        self.assertFalse(completion_status(session,fixture.cli.output)['ready'])
        calls=len(seen)
        _,again=fixture.flow.resume(before.session_id)
        self.assertEqual(len(seen),calls)
        self.assertEqual(again['usage']['requests_attempted'],session.usage.requests_attempted)

    def test_real_budget_boundary_preserves_unfinished_batches_without_false_success(self):
        """Exhaust the available digest allowance through the actual production graph.

        Args:
            self: Test owner giving the large population 98 rather than 220 requests.
        Returns:
            None; the real input limit still triggers batching, incomplete units
            remain visible, no global success is invented and no budget is added.
        """
        fixture,session,_,seen=self.run_contract(count=36,large=True,fault='budget',budget=98)
        self.assertTrue(any(t=='BoundedDigest' for t,_ in seen))
        progress=session.coverage['batch_progress']
        self.assertEqual(progress['mode'],'bounded_tree')
        self.assertLess(progress['completed'],progress['total'])
        self.assertFalse(completion_status(session,fixture.cli.output)['ready'])
        self.assertNotEqual(session.workflow['stop_reason'],'criteria_satisfied')
        self.assertLessEqual(session.usage.requests_attempted,98)
        self.assertEqual(session.request.budget.max_requests,98)

    def test_summary_conflict_blocks_optimistic_global_completion(self):
        """Carry an explicit leaf conflict through real reducers and final validation.

        Args:
            self: CLI fixture owner; only transport replies are synthetic.
        Returns:
            None; an optimistic global response cannot override the tree's blocker.
            This tests propagation, not whether a live model detects every conflict.
        """
        fixture, session, _, seen = self.run_contract(count=33, large=True, fault='summary_conflict', budget=150)
        last = session.coverage['history'][-1]
        self.assertEqual(last['input_mode'], 'bounded_tree')
        self.assertEqual(last['status'], 'assessed')
        self.assertIn('unresolved_conflict', last['summary_blockers'])
        self.assertTrue(any(title == 'BoundedDigest' and p['stage'] == 'reduction' for title,p in seen))
        self.assertTrue(all(row['status']=='partial' for row in last['criteria']))
        self.assertFalse(completion_status(session, fixture.cli.output)['ready'])
