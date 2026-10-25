"""Prove initial scope conversion and failure diagnostics through the real planning path.

Model HTTP replies are synthetic. The CLI, provider schema serialization, parser,
normalization and saved-plan loading are real; no paid requests are made.
"""
import json
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from projectclarity.nodes.plan_research.planning_failure_diagnostics import describe_planning_failure
from projectclarity.research_schemas import ResearchPlan, ResearchPlanOutput, ScopeEstimate
from tests.planning.planning_fixtures import planning_reply
from tests.planning import test_planning_cli_integration as planning_tests
from tests.planning import test_planning_failure_diagnostics as diagnostic_tests


class InitialScopeContractTests(unittest.TestCase):
    """Keep model judgments separate from application-owned evidence metadata."""

    def test_real_planner_saves_numeric_unknown_and_qualitative_estimates(self):
        """Exercise the actual provider schema and persistence with four estimate forms.

        Args:
            self: Test case running the real CLI with an isolated synthetic response.

        Returns:
            None. Fail if evidence bookkeeping reaches the model schema, if Python
            invents bounds or scale, or if normalized metadata fails to persist.
        """
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        answer = planning_reply(cli.plan)
        template = answer['scope_estimates'][0]
        variations = [({'lower': 10, 'upper': 200}, 'medium', 'provisional_prior'),
                      (None, 'unknown', 'unknown'),
                      (None, 'large', 'provisional_prior'),
                      ({'lower': 0, 'upper': 0}, 'unknown', 'provisional_prior')]
        answer['scope_estimates'] = []
        for index, (bounds, scale, _) in enumerate(variations, 1):
            answer['scope_estimates'].append(dict(template, estimate_id=f'E{index}', range=bounds, scale=scale))
        code, _, error = cli.run_cli(lambda request: cli.response(text=json.dumps(answer)))
        self.assertEqual(code, 0, error)
        schema = json.loads(cli.requests[0].content)['output_config']['format']['schema']
        scope = schema['$defs']['InitialScopeEstimate']
        self.assertNotIn('basis', scope['properties'])
        self.assertNotIn('candidate_ids', scope['properties'])
        self.assertNotIn('ScopeEstimate', schema['$defs'])
        bounds_schema = schema['$defs']['PlanningScopeRange']
        self.assertEqual(set(bounds_schema['required']), {'lower', 'upper'})
        self.assertEqual(bounds_schema['properties']['lower']['type'], 'integer')
        directory, session = cli.saved_session()
        self.assertEqual(len(cli.requests), 1)
        restored = ResearchPlan.model_validate_json((directory / 'plan.json').read_text())
        self.assertEqual(restored.model_dump(mode='json'), session['plan'])
        for estimate, (bounds, scale, basis) in zip(restored.scope_estimates, variations, strict=True):
            self.assertEqual(estimate.lower, bounds['lower'] if bounds is not None else None)
            self.assertEqual(estimate.upper, bounds['upper'] if bounds is not None else None)
            self.assertEqual(estimate.scale, scale)
            self.assertEqual(estimate.basis, basis)
            self.assertEqual(estimate.candidate_ids, [])

    def test_actual_parser_explains_bad_ranges_without_retry_or_retrieval(self):
        """Keep malformed ranges rejected with specific, safe field-level diagnostics.

        Args:
            self: Test case using the production CLI/SDK failure harness.

        Returns:
            None. Fail if missing, reversed or negative bounds become a valid plan,
            if the diagnostic becomes generic, or if the failure triggers more work.
        """
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        for bounds, code, location in [
            ({'lower': 10}, 'missing', 'scope_estimates[0].range.upper'),
            ({'lower': 20, 'upper': 10}, 'reversed_scope_range', 'scope_estimates[0].range'),
            ({'lower': -1, 'upper': 10}, 'greater_than_equal', 'scope_estimates[0].range.lower'),
        ]:
            with self.subTest(bounds=bounds):
                answer = planning_reply(cli.plan)
                answer['scope_estimates'][0]['range'] = bounds
                diagnostic, _, _ = diagnostic_tests.PlanningFailureIntegrationTests().run_rejected_reply(text=json.dumps(answer))
                self.assertEqual(diagnostic['issues'][0]['code'], code)
                self.assertEqual(diagnostic['issues'][0]['path'], location)

    def test_planner_cannot_supply_evidence_metadata(self):
        """Reject model-added evidence fields instead of silently laundering them into a prior.

        Args:
            self: Test case inspecting initial output validation without a model call.

        Returns:
            None. Fail if a planner can submit basis or candidate_ids, which belong
            to Python during initial planning.
        """
        fixture = json.loads((Path(__file__).parent / 'fixtures/valid_research_plan.json').read_text())
        for field, value in [('basis', 'evidence_informed'), ('candidate_ids', ['PRIVATE_INVENTED_ID'])]:
            with self.subTest(field=field):
                answer = planning_reply(fixture)
                answer['scope_estimates'][0][field] = value
                with self.assertRaises(ValidationError) as caught:
                    ResearchPlanOutput.model_validate(answer)
                failure = describe_planning_failure(caught.exception)
                self.assertEqual(failure.issues[0].code, 'extra_forbidden')
                self.assertNotIn('PRIVATE_INVENTED_ID', failure.model_dump_json())

    def test_legacy_scope_rules_have_distinct_safe_diagnostics(self):
        """Preserve later-assessment checks while naming the exact failed rule.

        Args:
            self: Test case exercising all five previously conflated scope rules.

        Returns:
            None. Fail if any rule is relaxed, loses its distinct code, or exposes
            private estimate text in the diagnostic output.
        """
        fixture = json.loads((Path(__file__).parent / 'fixtures/valid_research_plan.json').read_text())
        estimate = deepcopy(fixture['scope_estimates'][0])
        estimate['rationale'] = 'PRIVATE_SCOPE_CONTENT'
        for changes, code in [
            ({'upper': None}, 'incomplete_scope_range'),
            ({'lower': 300}, 'reversed_scope_range'),
            ({'basis': 'evidence_informed'}, 'missing_scope_evidence'),
            ({'candidate_ids': ['PRIVATE_ID']}, 'unexpected_scope_evidence'),
            ({'basis': 'unknown'}, 'inconsistent_unknown_scope'),
        ]:
            with self.subTest(code=code):
                with self.assertRaises(ValidationError) as caught:
                    ScopeEstimate.model_validate(dict(estimate, **changes))
                failure = describe_planning_failure(caught.exception)
                self.assertEqual(failure.issues[0].code, code)
                self.assertNotIn('PRIVATE', failure.model_dump_json())
