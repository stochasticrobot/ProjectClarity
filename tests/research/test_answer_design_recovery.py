"""Replay actual rejected designs through the public CLI with offline SDK transport.

Fixtures are exact design replies from two failed runs, not successful model
judgments. Corrections are explicitly synthetic. The graph, SDK schema/parser,
budget accounting, receipt persistence and plan binding remain production code.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from projectclarity.answer_schema.contracts import AnswerSchema
from anthropic import transform_schema
from projectclarity.nodes.design_answer.node import design_answer
from projectclarity.providers.validation_diagnostics import validation_issues, unresolved_failures
from projectclarity.research_schemas import ResearchSession
from tests.planning import test_planning_cli_integration as planning
from tests.research import test_answer_controller_cli as controller
from tests.research.test_contract_completion import explicit_specification

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'answer_design'


class AnswerDesignRecoveryTests(unittest.TestCase):
    """Verify rejected output is repaired explicitly or remains a bounded failure."""

    def fixture(self, name):
        """Load an intact failed proposal without depending on private run folders.

        Args:
            self: Test owner.
            name: Committed failure fixture basename.
        Returns:
            A JSON proposal that still fails the persisted contract validator.
        """
        return json.loads((FIXTURES / (name + '.json')).read_text())

    def run_case(self, proposal, *, budget=4, correct=True, stop_reason='end_turn', crash=False, cancel=False):
        """Drive the real CLI with malformed then optionally corrected SDK responses.

        Args:
            self: Test owner registering temporary harness cleanup.
            proposal: Exact rejected design; never altered in its first response.
            budget: Shared allowance, including design, correction and planning.
            correct: Whether the second response fixes the target metadata.
            stop_reason: First response completion status, including truncation.
            crash: Interrupt the second dispatch after its request is charged.
            cancel: Raise KeyboardInterrupt at the correction dispatch.
        Returns:
            Harness, terminal result, run directory, saved session and design inputs.
            All networking is blocked; an injected SystemExit persists uncertainty.
        """
        cli = planning.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        cli.plan['goals'][1]['requirement_ids'] = ['C2']
        inputs = []

        def provider(request):
            """Return saved bad output, a synthetic correction, then a fixture plan.

            Args:
                request: Real serialized provider request produced by the SDK.
            Returns:
                Synthetic provider HTTP response; optionally raises SystemExit at
                the correction boundary to test uncertain-call persistence.
            """
            wire = json.loads(request.content)
            if wire['output_config']['format']['schema']['title'] != 'AnswerSchema':
                return cli.response()
            inputs.append(json.loads(wire['messages'][0]['content']))
            if crash and len(inputs) == 2:
                raise SystemExit(77)
            if cancel and len(inputs) == 2:
                raise KeyboardInterrupt
            reply = copy.deepcopy(proposal)
            if len(inputs) == 2 and correct:
                for check in reply['completion_checks']:
                    check['target_basis'] = 'research' if check['kind'] == 'enumeration' else 'not_applicable'
            return cli.response(text=json.dumps(reply),
                stop_reason=stop_reason if len(inputs) == 1 else 'end_turn')

        result = cli.run_cli(provider, '--design-answer', '--max-requests', str(budget),
            '--question', 'Identify all current Members of Parliament, federally, in Canada.',
            plan_only=True, details=False)
        directory, saved = cli.saved_session()
        return cli, result, directory, saved, inputs

    def test_both_live_failures_reject_then_correct_through_production(self):
        """Reproduce both actual failures and reach planning only after correction.

        Args:
            self: Test owner with offline production CLI fixtures.
        Returns:
            None; asserts immutable first replies, safe feedback, three charged
            requests and an accepted corrected contract. Not live quality proof.
        """
        for name in ['enumeration_without_target', 'qualitative_with_target']:
            with self.subTest(name=name):
                proposal = self.fixture(name)
                with self.assertRaises(ValidationError):
                    AnswerSchema.model_validate(proposal)
                cli, result, directory, saved, inputs = self.run_case(proposal)
                self.assertEqual(result[0], 0, result[2])
                self.assertIn('plan_research', saved['visited_nodes'])
                self.assertEqual(saved['usage']['requests_attempted'], 3)
                self.assertEqual(len(inputs), 2)
                correction = inputs[1]['correction']
                self.assertEqual(json.loads(correction['previous_response']), proposal)
                self.assertTrue(correction['validation_issues'])
                first = json.loads((directory / 'llm_calls/answer_design.json').read_text())
                second = json.loads((directory / 'llm_calls/answer_design_correction.json').read_text())
                self.assertEqual(first['error_code'], 'invalid_output_schema')
                self.assertEqual(json.loads(first['response_text']), proposal)
                self.assertEqual(second['status'], 'completed')
                self.assertEqual(saved['answer_schema'], second['parsed_result'])
                # Inspect what the SDK actually sent, not merely Pydantic's schema.
                wire = json.loads(cli.requests[0].content)['output_config']['format']['schema']
                accepted = json.loads((FIXTURES / 'provider_accepted_schema.json').read_text())
                current = AnswerSchema.model_json_schema()
                from copy import deepcopy
                baseline_shape = deepcopy(current)
                multiplicity = baseline_shape['$defs']['ReportField']['properties'].pop('multiplicity')
                self.assertEqual(multiplicity, dict(default='single',
                    description='single requires one resolved value; multiple collects compatible supported values. Identity fields must be single.',
                    enum=['single', 'multiple'], title='Multiplicity', type='string'))
                self.assertEqual(baseline_shape, accepted)
                self.assertEqual(wire, transform_schema(current))
                self.assertEqual(saved['workflow']['model_call_resolutions'],
                                 {'answer_design': 'answer_design_correction'})

    def test_repeated_invalid_budget_and_incomplete_output_remain_closed(self):
        """Bound correction and never accept truncated or uncorrected output.

        Args:
            self: Test owner invoking independent production CLI runs.
        Returns:
            None; each failure preserves its binding allowance and absent plan.
        """
        for options, calls in [({'correct': False}, 2), ({'budget': 2}, 1),
                               ({'stop_reason': 'max_tokens'}, 1), ({'cancel': True}, 2)]:
            with self.subTest(options=options):
                _, result, _, saved, inputs = self.run_case(
                    self.fixture('enumeration_without_target'), **options)
                self.assertNotEqual(result[0], 0)
                self.assertEqual(saved['usage']['requests_attempted'], calls)
                self.assertEqual(len(inputs), calls)
                self.assertIsNone(saved['answer_schema'])
                self.assertNotIn('plan_research', saved['visited_nodes'])

    def test_interrupted_correction_never_replays(self):
        """Keep a charged interrupted correction uncertain rather than sending again.

        Args:
            self: Test owner injecting a process exit at the real SDK boundary.
        Returns:
            None; production initialization cannot replay the started design even
            when invoked again with its saved state. Existing receipts stay intact.
        """
        cli, result, directory, saved, _ = self.run_case(
            self.fixture('qualitative_with_target'), crash=True)
        self.assertEqual(result[0], 77)
        self.assertEqual(saved['usage']['requests_attempted'], 2)
        before = (directory / 'llm_calls/answer_design.json').read_bytes()
        session = ResearchSession.model_validate(saved)
        with patch('projectclarity.nodes.design_answer.node.invoke_structured',
                   side_effect=AssertionError('Uncertain design replayed')):
            design_answer({'session': session}, cli.output, lambda: 'unused')
        self.assertIsNone(session.answer_schema)
        self.assertIn('not replayed', session.error)
        self.assertEqual((directory / 'llm_calls/answer_design.json').read_bytes(), before)

    def test_corrected_design_continues_through_reading_review_and_completion(self):
        """Exercise the repair beyond planning using a different source population.

        Args:
            self: Test owner creating the existing transport-only controller fixture.
        Returns:
            None; real production scheduling, retrieval and review fill eight rows
            and satisfy the contract, retaining the rejected design receipt.
        """
        case = controller.AnswerControllerCLITests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.count = 8
        case.schema = explicit_specification(case.count, supporting=True)
        case.install_sources(271)
        original = case.flow.provider
        rejected = []

        def provider(request):
            """Inject a single invalid design before normal source-derived replies.

            Args:
                request: Real SDK request intercepted at the network boundary.
            Returns:
                One inconsistent proposal or the existing synthetic fixture reply.
            """
            wire = json.loads(request.content)
            if wire['output_config']['format']['schema']['title'] == 'AnswerSchema' and not rejected:
                bad = copy.deepcopy(case.schema)
                bad['completion_checks'][0]['target_basis'] = 'not_applicable'
                rejected.append(bad)
                return case.cli.response(text=json.dumps(bad))
            return original(request)

        case.flow.provider = provider
        code, directory, saved, err = case.run_case()
        self.assertEqual(code, 0, err)
        self.assertEqual(saved['workflow']['stop_reason'], 'criteria_satisfied')
        report = json.loads((directory / 'answer.json').read_text())
        self.assertEqual(report['progress']['goals'][0]['results']['supported'], 8)
        session = ResearchSession.model_validate(saved)
        self.assertEqual(unresolved_failures(session), [])
        first = next(c for c in session.processing_calls if c['call_id'] == 'answer_design')
        self.assertEqual(first['status'], 'failed')
        # Resolution is explicit and must point to a completed same-task call.
        for replacement in ['missing', 'answer_design', 'planning']:
            session.workflow['model_call_resolutions']['answer_design'] = replacement
            self.assertIn(first, unresolved_failures(session))

    def test_diagnostic_feedback_does_not_leak_unknown_names_or_values(self):
        """Keep arbitrary rejected values out of correction diagnostics.

        Args:
            self: Test owner checking the shared diagnostic boundary.
        Returns:
            None; only known schema paths and application-authored messages remain.
        """
        bad = self.fixture('qualitative_with_target')
        bad['PRIVATE_FIELD'] = 'PRIVATE_VALUE'
        try:
            AnswerSchema.model_validate(bad)
        except ValidationError as exc:
            issues = validation_issues(exc, AnswerSchema)
        self.assertNotIn('PRIVATE', json.dumps(issues))
        self.assertLessEqual(len(issues), 8)
