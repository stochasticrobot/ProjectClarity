"""Prove answer-owned stopping through production paths with synthetic transports.

Tests retain the real graph, SDK serialization, parsing, source validation,
scheduler, budgets and saved artifacts. Replies are controlled evidence judgments,
not live-model accuracy measurements. Large-source coverage uses the default
512,000-character aggregate limit, not a lowered test-only context threshold.
"""
from tests.claim_review.mapping_fixtures import fixture_mappings
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

    def run_contract(self, *, count=2, format='records', large=False, fault=None, budget=220):
        """Run a designed report through the actual terminal entry point.

        Args:
            self: Test owner registering transport fixtures and cleanup.
            count: Number of deterministic unseen fixture records to put in the source.
            format: Report kind; comparisons/narratives use nonnumeric completion.
            large: Include long complete quotations and qualifications so final
                evidence really exceeds the production aggregate input allowance.
            fault: Optional wrong_unit, missing_field, conflict, unknown_target,
                digest_invalid, or digest_crash fault at the provider boundary.
                budget accepts an ordinary incomplete outcome under a lower allowance.
            budget: Explicit shared request allowance; never changed by production code.
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
        numeric = None if format in {'comparison','narrative'} else count
        schema = explicit_specification(numeric, format, supporting=True)
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
                    report_mappings=fixture_mappings(c, payload), evidence_quotes=[c['proposal']['evidence'][0]['quote']]) for c in payload['candidates']])
            elif title == 'EvidenceDigest':
                if fault == 'digest_crash' and not crashes:
                    crashes.append(payload['candidates'][0]['candidate_id'])
                    raise SystemExit(77)
                answer = dict(findings=[dict(statement=c['proposal']['statement'],
                    candidate_ids=[c['candidate_id']], criterion_ids=['A1','A2'], limitations=[])
                    for c in payload['candidates']])
                if fault == 'digest_invalid':
                    answer['findings'][0]['candidate_ids']=['invented_candidate']
            elif title == 'CoverageAssessment':
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
        if fault == 'digest_crash':
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

    def test_comparison_and_narrative_stop_without_a_population_count(self):
        """Exercise nonnumeric contracts through the public terminal workflow.

        Args:
            self: Test owner running both report shapes with controlled judgments.
        Returns:
            None; checks production stopping, separate supporting units, source
            context delivery and the dashboard's identical completion projection.
        """
        for format in ('comparison','narrative'):
            with self.subTest(format=format):
                fixture,session,directory,seen = self.run_contract(format=format)
                self.assertEqual(session.workflow['stop_reason'],'criteria_satisfied')
                self.assertTrue(completion_status(session,fixture.cli.output)['ready'])
                self.assertTrue(all(c['target_count'] is None for c in session.coverage['criteria_progress']))
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
        self.assertGreater(last['direct_input_characters'],512_000)
        self.assertEqual(last['input_mode'],'incremental')
        from projectclarity.coverage.evidence_batches import fingerprint
        digests=Counter(fingerprint(p) for t,p in seen if t=='EvidenceDigest')
        self.assertTrue(digests)
        self.assertEqual(max(digests.values()),1)
        batch=session.coverage['batch_progress']
        self.assertGreater(batch['total'],1)
        self.assertEqual(batch['completed'],batch['total'])
        self.assertEqual(batch['candidates_reviewed'],len(session.extraction['candidates']))
        final=next(p for title,p in reversed(seen) if title=='CoverageAssessment')
        self.assertEqual(final['candidates'],[])
        self.assertEqual(len(final['evidence_batches']),batch['total'])
        represented={cid for b in final['evidence_batches'] for f in b['findings'] for cid in f['candidate_ids']}
        self.assertEqual(represented,{c['candidate_id'] for c in session.extraction['candidates']})
        self.assertEqual(len(final['result_inventory']['G1']['items']),36)
        self.assertTrue(completion_status(session,fixture.cli.output)['ready'])
        from projectclarity.dashboard.projection import snapshot
        self.assertEqual(snapshot(fixture.cli.output,directory.name)['completion']['batch_progress']['completed'],batch['total'])
        changed=session.model_copy(deep=True)
        changed.coverage['evidence_batches'][batch['batch_ids'][0]]['status']='failed'
        self.assertFalse(completion_status(changed,fixture.cli.output)['ready'])
        changed=session.model_copy(deep=True)
        changed.coverage['evidence_batches'][batch['batch_ids'][0]]['assessment']['findings'][0]['statement']='Altered digest'
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
            allowance and gets at most two logical attempts. Completion stays blocked.
        """
        fixture,session,_,seen=self.run_contract(count=36,large=True,fault='digest_invalid')
        receipts=session.coverage['evidence_batches']
        self.assertTrue(receipts)
        self.assertTrue(all(r['status']=='failed' and r['attempts']<=2 for r in receipts.values()))
        self.assertTrue(any(r['attempts']==2 for r in receipts.values()))
        self.assertFalse(completion_status(session,fixture.cli.output)['ready'])
        self.assertLessEqual(session.usage.requests_attempted,session.request.budget.max_requests)
        self.assertTrue(any(t=='EvidenceDigest' for t,_ in seen))

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
        first=next(p for t,p in seen if t=='EvidenceDigest')
        first_ids=[c['candidate_id'] for c in first['candidates']]
        _,saved=fixture.flow.resume(before.session_id)
        session=ResearchSession.model_validate(saved)
        receipt=session.coverage['evidence_batches'][reserved[0]['batch_id']]
        self.assertEqual(receipt['status'],'uncertain')
        self.assertEqual(receipt['attempts'],1)
        self.assertEqual(sum([c['candidate_id'] for c in p['candidates']]==first_ids
            for t,p in seen if t=='EvidenceDigest'),1)
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
        self.assertTrue(any(t=='EvidenceDigest' for t,_ in seen))
        progress=session.coverage['batch_progress']
        self.assertEqual(progress['mode'],'incremental')
        self.assertLess(progress['completed'],progress['total'])
        self.assertFalse(completion_status(session,fixture.cli.output)['ready'])
        self.assertNotEqual(session.workflow['stop_reason'],'criteria_satisfied')
        self.assertLessEqual(session.usage.requests_attempted,98)
        self.assertEqual(session.request.budget.max_requests,98)
