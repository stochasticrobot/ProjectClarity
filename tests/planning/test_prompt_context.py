"""Check that guidance remains interpretable across current and legacy review records."""
import unittest

from projectclarity.prompt_context import research_context
from tests.research.research_test_fixtures import example_session


class PromptContextTests(unittest.TestCase):
    """Guard against attaching a short answer to a missing or replacement question."""

    def test_legacy_answer_uses_only_the_original_plan_question(self):
        """Recover legacy question text only when the reviewed plan was not replaced.

        Args:
            self: Test case with isolated synthetic session state.

        Returns:
            None. Fail if a replacement plan supplies invented context for an old answer.
        """
        session = example_session()
        session.plan.clarifications = ["Include historical members?"]
        session.review_decision = {"answers": [{"question_id": "Q1", "answer": "No."}]}
        self.assertEqual(research_context(session)["human_guidance"],
            [{"question": "Include historical members?", "answer": "No."}])
        session.review_decision["revised_plan"] = session.plan.model_dump(mode="json")
        self.assertEqual(research_context(session)["human_guidance"],
            [{"question": None, "answer": "No."}])

    def test_bound_question_and_assumptions_survive_replacement(self):
        """Expose original question text and current assumptions without internal IDs.

        Args:
            self: Test case with isolated current-format human guidance.

        Returns:
            None. Fail if original question meaning, the date, or the human note is lost.
        """
        session = example_session()
        session.plan.assumptions = ["Count current members only."]
        session.review_decision = {"revised_plan": session.plan.model_dump(mode="json"),
            "answers": [{"question_id": "Q1", "question": "Include historical members?", "answer": "No."}],
            "note": "Include independent researchers."}
        context = research_context(session)
        self.assertEqual(context["human_guidance"],
            [{"question": "Include historical members?", "answer": "No."}])
        self.assertEqual(context["working_assumptions"], session.plan.assumptions)
        self.assertEqual(context["research_date"], "2026-10-21")
        self.assertEqual(context["human_note"], "Include independent researchers.")
