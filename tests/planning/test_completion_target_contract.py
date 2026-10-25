"""Check completion-target rules in the actual provider schema, not just Python.

Synthetic HTTP replies exercise the production CLI, SDK serialization, parser and
saved-plan conversion without paid calls. These tests do not establish live plan quality.
"""
import json
import unittest
from copy import deepcopy

from pydantic import ValidationError

from projectclarity.research_schemas import ResearchPlan, ResearchPlanOutput
from tests.planning.planning_fixtures import planning_reply
from tests.planning import test_planning_cli_integration as planning_tests


class CompletionTargetContractTests(unittest.TestCase):
    """Prevent a provider-valid count/basis combination from failing only after generation."""

    def test_extra_search_proposals_survive_real_cli_without_execution(self):
        """Preserve more than five queued queries instead of rejecting a useful plan.

        Args:
            self: Test case invoking the production planning CLI with synthetic HTTP.

        Returns:
            None. Fail if extra proposals are rejected, truncated, or executed in
            plan-only mode; the global request allowance must remain unchanged.
        """
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        queries = [f'AI security Canada research facet {index}' for index in range(7)]
        cli.plan['strategy'][0]['proposed_searches'] = queries
        code, _, error = cli.run_cli(lambda request: cli.response())
        self.assertEqual(code, 0, error)
        _, session = cli.saved_session()
        self.assertEqual(session['plan']['strategy'][0]['proposed_searches'], queries)
        self.assertEqual(session['request']['budget']['max_requests'], 1)
        self.assertEqual(session['usage']['requests_attempted'], 1)
        self.assertEqual(session['visited_nodes'], ['initialize_research', 'plan_research'])
        self.assertEqual(session['search'], {})

    def test_provider_schema_and_real_cli_accept_only_consistent_target_shapes(self):
        """Inspect the schema actually sent by the SDK and persist all valid target forms.

        Args:
            self: Test case with a temporary CLI run and synthetic provider transport.

        Returns:
            None. Fail if any schema branch permits a non-user count, if requested
            counts become nullable, or if conversion changes criterion content.
        """
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        answer = planning_reply(cli.plan)
        template = answer['completion_criteria'][0]
        answer['completion_criteria'] = [
            dict(template, criterion_id=f'K{index}', target={'requested_count': count} if basis == 'user_request' else basis)
            for index, (basis, count) in enumerate([
                ('research', None), ('not_applicable', None), ('user_request', 3),
                ('research', None),
            ], 1)
        ]
        code, _, error = cli.run_cli(lambda request: cli.response(text=json.dumps(answer)))
        self.assertEqual(code, 0, error)
        self.assertEqual(len(cli.requests), 1)
        schema = json.loads(cli.requests[0].content)['output_config']['format']['schema']
        items = schema['properties']['completion_criteria']['items']
        # Only the target may branch; duplicating the whole criterion exceeded the
        # live provider's grammar limit even though the local SDK serialized it.
        self.assertNotIn('anyOf', items)
        criterion = schema['$defs'][items['$ref'].rsplit('/', 1)[1]]
        properties = criterion['properties']
        self.assertNotIn('target_basis', properties)
        self.assertNotIn('target_count', properties)
        self.assertEqual(set(criterion['required']), set(properties))
        branches = properties['target']['anyOf']
        self.assertEqual(len(branches), 2)
        enums = next(branch for branch in branches if branch.get('type') == 'string')
        self.assertEqual(set(enums['enum']), {'research', 'not_applicable'})
        requested_ref = next(branch['$ref'] for branch in branches if '$ref' in branch)
        requested = schema['$defs'][requested_ref.rsplit('/', 1)[1]]
        self.assertEqual(set(requested['properties']), {'requested_count'})
        self.assertEqual(requested['required'], ['requested_count'])
        self.assertEqual(requested['properties']['requested_count']['type'], 'integer')
        directory, session = cli.saved_session()
        self.assertEqual(session['prompt_version'], 'research-plan-v11')
        expected = []
        for item in answer['completion_criteria']:
            stored = dict(item)
            target = stored.pop('target')
            stored['target_basis'] = 'user_request' if isinstance(target, dict) else target
            stored['target_count'] = target['requested_count'] if isinstance(target, dict) else None
            expected.append(stored)
        self.assertEqual(session['plan']['completion_criteria'], expected)
        restored = ResearchPlan.model_validate_json((directory / 'plan.json').read_text())
        self.assertEqual(restored.model_dump(mode='json'), session['plan'])

    def test_malformed_fourth_criterion_cannot_bypass_local_validation(self):
        """Retain the safety check even when a provider violates its structured schema.

        Args:
            self: Test case validating synthetic variants at the reported fourth slot.

        Returns:
            None. Fail if a research/nonnumeric count or missing requested count is
            silently accepted, repaired, or removed by planning conversion.
        """
        cli = planning_tests.PlanningCLIIntegrationTests()
        cli.setUp()
        self.addCleanup(cli.doCleanups)
        answer = planning_reply(cli.plan)
        template = answer['completion_criteria'][0]
        answer['completion_criteria'] = [dict(template, criterion_id=f'K{i}') for i in range(1, 5)]
        for target in [{'target_basis': 'research', 'target_count': 4}, {'requested_count': None}, {}, 4, 'user_request']:
            with self.subTest(target=target):
                malformed = deepcopy(answer)
                malformed['completion_criteria'][3]['target'] = target
                with self.assertRaises(ValidationError) as caught:
                    ResearchPlanOutput.model_validate(malformed)
                self.assertTrue(all(error['loc'][:2] == ('completion_criteria', 3)
                                    for error in caught.exception.errors()))
