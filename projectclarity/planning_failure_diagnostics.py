"""Explain why a planner reply was rejected without recording the reply itself.

For example, report "question_category: required field is missing" instead of
just "invalid plan". LangChain wraps Pydantic's validation errors, so this module
looks through that exception chain for structured field locations and rule codes.
It never copies exception messages, rejected values, unknown field names, model
text, or tracebacks: those can contain private research data or credentials.

agentic_workflow.py calls describe_planning_failure() after a failed parse, saves
its versioned record as planning_failure.json, and puts a short explanation in
session.json and the terminal. The monitor receives only the failure category.
This is diagnosis, not repair: no answer is changed, no rule is relaxed, and no
additional model request is made. An old run without these details cannot be
diagnosed retroactively. At most eight issues are recorded per failed reply.
"""

import json
from typing import Literal

from pydantic import Field, ValidationError

from projectclarity.research_schemas import Contract, Goal, ResearchPlan, StrategyStep


# All displayed wording is ours, never a provider/validator exception message.
# Unknown codes get a generic explanation rather than leaking their text.
RULE_MESSAGES = {
    "missing": "Required field is missing.",
    "extra_forbidden": "An unexpected field was supplied; its name is omitted.",
    "string_type": "Expected text.",
    "list_type": "Expected a list.",
    "model_type": "Expected an object with the required fields.",
    "string_too_short": "Text is empty or shorter than the schema permits.",
    "string_too_long": "Text exceeds the schema length limit.",
    "string_pattern_mismatch": "Identifier does not match the required format.",
    "too_short": "List has fewer entries than the schema requires.",
    "too_long": "List has more entries than the schema permits.",
    "duplicate_goal_ids": "Goal identifiers must be unique.",
    "strategy_unknown_goals": "A strategy step refers to a goal not defined in goals.",
    "strategy_uncovered_goals": "At least one goal has no strategy step covering it.",
    "validation_error": "A schema validation rule failed.",
}
KNOWN_FIELDS = set(ResearchPlan.model_fields) | set(Goal.model_fields) | set(StrategyStep.model_fields)
MAX_ISSUES = 8


class PlanningValidationIssue(Contract):
    """An application-authored location/rule explanation, never a rejected value."""

    path: str = Field(max_length=240)
    code: str = Field(max_length=64)
    message: str = Field(max_length=200)


class PlanningFailure(Contract):
    """Small, versioned diagnostic artifact; separate from a successful plan."""

    schema_version: Literal["1.0"] = "1.0"
    error_code: Literal["plan_schema_invalid", "plan_json_invalid", "plan_parse_failed", "plan_result_missing"]
    issues: list[PlanningValidationIssue] = Field(default_factory=list, max_length=MAX_ISSUES)
    issues_truncated: bool = False

    def explanation(self) -> str:
        """Explain the first few problems concisely; the artifact retains up to eight."""
        if self.issues:
            summary = "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues[:3])
            if len(self.issues) > 3 or self.issues_truncated:
                summary += " See planning_failure.json for the bounded issue list."
            return summary
        return {
            "plan_json_invalid": "The response could not be parsed as JSON.",
            "plan_parse_failed": "The response parser failed without usable field-level details.",
            "plan_result_missing": "The response parser returned no structured plan.",
            "plan_schema_invalid": "The response did not satisfy the plan schema.",
        }[self.error_code]


def safe_field_path(location: tuple) -> str:
    """Keep known field names and list positions; hide model-invented field names."""
    path = ""
    for part in location[:8]:
        if type(part) is int and 0 <= part <= 10000:
            path += f"[{part}]"
        else:
            label = part if isinstance(part, str) and part in KNOWN_FIELDS else "<unknown-field>"
            path += ("." if path else "") + label
    return path[:240] or "plan"


def describe_planning_failure(error: Exception | None) -> PlanningFailure:
    """Use structured causes, never parse an exception's potentially private text."""
    if error is None:
        return PlanningFailure(error_code="plan_result_missing")
    seen = set()
    current = error
    # Wrappers normally have one cause. Bound traversal and handle cycles so a
    # diagnostic failure cannot hang the worker or trigger another model call.
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ValidationError):
            errors = current.errors(include_input=False, include_context=False, include_url=False)
            issues = []
            for item in errors[:MAX_ISSUES]:
                code = item["type"] if item["type"] in RULE_MESSAGES else "validation_error"
                issues.append(PlanningValidationIssue(path=safe_field_path(item["loc"]),
                    code=code, message=RULE_MESSAGES[code]))
            return PlanningFailure(error_code="plan_schema_invalid", issues=issues,
                                   issues_truncated=len(errors) > MAX_ISSUES)
        if isinstance(current, json.JSONDecodeError):
            return PlanningFailure(error_code="plan_json_invalid")
        current = current.__cause__ or current.__context__
    return PlanningFailure(error_code="plan_parse_failed")
