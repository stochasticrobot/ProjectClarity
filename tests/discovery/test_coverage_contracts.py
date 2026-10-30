"""Check that numerical matches and plausible IDs cannot bypass evidence requirements.

The tests construct coverage replies and call the same validators used after model
assessment. They cover unknown totals, missing evidence, duplicate items, partial
input and different goal associations. No model supplies these judgments; passing
proves the exercised validation rules, not research completeness.
"""
import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError
from projectclarity.coverage.coverage_contracts import CoverageAssessment
from projectclarity.nodes.assess_research_gaps.node import validate_assessment
from projectclarity.research_schemas import CompletionCriterion, ResearchPlanOutput, ResearchSession
from tests.research.research_test_fixtures import example_session


class CoverageContractTests(unittest.TestCase):
    """Unknown targets, duplicate groupings and nonnumeric tasks stay distinct."""

    def setUp(self):
        """Build a generic criterion and one offered evidence reference.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Initialize this test instance's fixtures; unittest calls setUp before each
            test method.
        """
        self.session = example_session()
        self.session.plan.completion_criteria = [CompletionCriterion(criterion_id="K1", goal_ids=["G1"],
            description="Identify all current members.", kind="enumeration", measurement_method="Compare against an authoritative roster.",
            target_basis="research", target_count=None)]
        self.offered = {"C1": SimpleNamespace(proposal=SimpleNamespace(goal_ids=["G1"]), adjudication_status="supported")}
        self.proposal = {"assessments": [{"criterion_id": "K1", "judgment": "satisfied",
            "items": [{"key": "Alice", "candidate_ids": ["C1"]}], "target_count": None,
            "benchmark_candidate_ids": [], "evidence_candidate_ids": ["C1"], "gaps": [],
            "rationale": "Fixture judgment, not real evidence."}], "next_searches": []}

    def check(self, *, truncated=False):
        """Run the same post-model validation used by the production coverage node.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.
            truncated: Whether eligible evidence was omitted from the model inventory; True
                blocks a satisfied coverage result.

        Returns:
            The criterion-progress dictionaries returned by the production coverage validator;
            invalid fixture proposals raise ValueError.
        """
        return validate_assessment(CoverageAssessment.model_validate(self.proposal), self.session, self.offered, truncated=truncated)

    def test_unknown_population_does_not_become_complete(self):
        """One observed item cannot prove coverage when the denominator is unknown.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        result = self.check()[0]
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["target_count"])

    def test_cross_goal_evidence_retains_progress_without_claiming_satisfaction(self):
        """Treat earlier goal tags as relevance metadata, not exclusive ownership of facts.

        Args:
            self: Fixture citing a valid offered claim under another research criterion.

        Returns:
            None. Assert the item and candidate IDs survive with an explicit relevance
            warning and partial status, while unknown evidence still fails validation.
        """
        self.offered['C1'].proposal.goal_ids = ['G2']
        result = self.check()[0]
        self.assertEqual(result['observed_count'], 1)
        self.assertEqual(result['cross_goal_candidate_ids'], ['C1'])
        self.assertIn('cross_goal_relevance_unconfirmed', result['blocking_reasons'])
        self.assertEqual(result['status'], 'partial')
        self.proposal['assessments'][0]['evidence_candidate_ids'].append('invented')
        with self.assertRaisesRegex(ValueError, 'unknown_or_changed_evidence'):
            self.check()

    def test_researched_count_requires_evidence(self):
        """A plausible numeric target without source evidence is rejected.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.proposal["assessments"][0]["target_count"] = 1
        with self.assertRaisesRegex(ValueError, "unsupported_denominator"):
            self.check()

    def test_duplicates_do_not_inflate_coverage(self):
        """Whitespace/case variations of an item key cannot count as two items.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.proposal["assessments"][0]["items"].append({"key": " ALICE ", "candidate_ids": ["C1"]})
        with self.assertRaisesRegex(ValueError, "duplicate_or_empty_item_key"):
            self.check()

    def test_truncation_and_gaps_block_satisfaction_even_with_matching_count(self):
        """Count equality is not enough if the model did not see all offered evidence or admits
        gaps.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        assessment = self.proposal["assessments"][0]
        assessment.update(target_count=1, benchmark_candidate_ids=["C1"], gaps=["Membership date still uncertain."])
        result = self.check(truncated=True)[0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(set(result["blocking_reasons"]), {"assessment_inventory_truncated", "unresolved_gaps"})

    def test_quote_collection_does_not_require_a_fabricated_total(self):
        """An evidence-sufficiency criterion can remain open-ended instead of inventing a
        population.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.session.plan.completion_criteria = [CompletionCriterion(criterion_id="K1", goal_ids=["G1"],
            description="Collect attributed quotations with source context and date.", kind="evidence_sufficiency",
            measurement_method="Check attribution and federal/provincial coverage; disclose unsearched archives.",
            target_basis="not_applicable", target_count=None)]
        self.proposal["assessments"][0].update(judgment="partial", gaps=["Provincial sources still need searching."])
        result = self.check()[0]
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["target_count"])

    def test_three_travel_alternatives_is_a_request_not_a_discovered_fact(self):
        """Requested deliverable counts and constraint checks are not roster denominators.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        self.session.plan.completion_criteria = [CompletionCriterion(criterion_id="K1", goal_ids=["G1"],
            description="Three distinct feasible travel plans respecting the requested route.", kind="constraints",
            measurement_method="Check route, two days in Scotland and connections for each alternative.",
            target_basis="user_request", target_count=3)]
        self.proposal["assessments"][0].update(target_count=2)
        with self.assertRaisesRegex(ValueError, "changed_requested_target"):
            self.check()
        self.proposal["assessments"][0].update(target_count=3, judgment="partial", gaps=["Connections not established."])
        self.assertEqual(self.check()[0]["status"], "partial")

    def test_planning_cannot_invent_researched_denominator(self):
        """Unknown research totals stay unset in the planning contract.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        criterion = self.session.plan.completion_criteria[0].model_dump()
        criterion["target_count"] = 300
        with self.assertRaises(ValidationError):
            CompletionCriterion.model_validate(criterion)

    def test_new_plan_requires_criteria_but_historical_session_remains_readable(self):
        """No silent loss of new criteria and no invented criteria when reading old saved work.

        Args:
            self: This unittest case instance, holding its isolated fixtures and assertion
                methods.

        Returns:
            None. Assertions fail if the documented behavior regresses; unittest records the
            result.
        """
        plan = self.session.plan.model_dump()
        del plan["completion_criteria"]
        with self.assertRaises(ValidationError):
            ResearchPlanOutput.model_validate(plan)
        old = json.loads(self.session.model_dump_json())
        old["plan"] = plan
        old["prompt_version"] = "research-plan-v4"
        restored = ResearchSession.model_validate(old)
        self.assertEqual(restored.plan.completion_criteria, [])
