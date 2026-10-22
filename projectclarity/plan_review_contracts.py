"""Define the questions a user sees and the decision a UI sends back.

For example, a terminal can print question Q1 while a web page renders Q1 as a
text box. Both submit the same PlanReviewDecision; neither decides which graph
node to run. A review ID binds a response to one particular plan, so an old
browser tab cannot accidentally approve a different proposal.

These versioned JSON contracts contain private research text. They belong in a
user-authorized review response, not general operational logs. This module only
validates data; plan_review_service.py checks it against the pending workflow.
"""

from typing import Literal

from pydantic import Field, model_validator

from projectclarity.research_schemas import Contract, Identifier, ResearchPlan, Text


class ReviewQuestion(Contract):
    """A stable field name and its displayed question, independent of the UI."""

    question_id: Identifier
    question: Text


class PlanReviewRequest(Contract):
    """The private payload returned when the graph needs a person's decision."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["plan_review"] = "plan_review"
    run_id: str
    review_id: str
    plan_sha256: str
    created_at: str
    plan: ResearchPlan
    questions: list[ReviewQuestion] = Field(max_length=8)
    allowed_actions: list[Literal["answer", "accept_assumptions", "reject"]] = Field(
        default_factory=lambda: ["answer", "accept_assumptions", "reject"])
    # Fixed policy is shown beside the proposal: answering a question never
    # expands URL permissions, raises budgets, or enables proposed web searches.
    execution_policy: Literal["supplied_sources_only"] = "supplied_sources_only"


class ReviewAnswer(Contract):
    """A person's clarification, preserved verbatim as human planning guidance."""

    question_id: Identifier
    answer: Text


class PlanReviewDecision(Contract):
    """A transport-neutral response; no API keys, tool calls, or budget overrides.

    'answer' requires every question to be answered. Answers supplement the
    original plan as explicit human guidance; they do not trigger another planning
    call. Opt-in extraction receives these answers alongside the plan as guidance,
    not as source evidence. A UI may also supply a complete revised_plan, which is
    validated before it replaces the active plan. The original remains in the
    review request. 'accept_assumptions' explicitly waives unanswered questions.
    """

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    review_id: str
    action: Literal["answer", "accept_assumptions", "reject"]
    answers: list[ReviewAnswer] = Field(default_factory=list, max_length=8)
    revised_plan: ResearchPlan | None = None
    note: Text | None = None

    @model_validator(mode="after")
    def check_action_fields(self):
        """Reject contradictory controls rather than guessing which one wins."""
        if self.action != "answer" and (self.answers or self.revised_plan is not None):
            raise ValueError("Only an answer decision may include answers or a revised plan")
        ids = [answer.question_id for answer in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("Question IDs must be unique")
        if self.revised_plan is not None and self.revised_plan.clarifications:
            raise ValueError("A revised plan must resolve its clarification questions")
        return self
