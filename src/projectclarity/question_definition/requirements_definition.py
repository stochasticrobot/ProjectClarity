"""Turn a research question into a checklist of what the answer must contain.

A question needs an answer -- what pieces of information need to appear in the answer?

Before looking for information, the application asks the AI model what it needs to
learn. For a question about Canadian AI companies, that might include a company
name, its connection to Canada and evidence of AI activity. This file prepares
that question and checks that the reply contains a usable list of requirements.

The setup workflow and the standalone test tool both use these rules. Later steps
receive the saved requirements to help select relevant facts, and the report uses
them as its answer fields. This file prepares and checks the request and reply;
the calling code sends the request to the AI service.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]


class InformationItem(BaseModel):
    """One question-specific information requirement, not an extracted fact."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["entity_type", "attribute", "relationship"]
    name: str
    description: str


class InformationPlan(BaseModel):
    """The fixed response structure for variable research requirements."""
    model_config = ConfigDict(extra="forbid", strict=True)
    information_needed: list[InformationItem]


def validate_plan(text):
    """Validate a planner response before it can influence extraction.

    Args:
        text: JSON response text or a saved plain requirements object encoded as JSON.

    Returns:
        InformationPlan with at least one fully described requirement. Raises
        ValueError on invalid JSON, extra/missing fields, invalid kinds or blanks.
        No model call, repair, normalization, or filesystem change occurs.
    """
    plan = InformationPlan.model_validate_json(text)
    if not plan.information_needed:
        raise ValueError("The planner returned no information requirements")
    if any(not item.name.strip() or not item.description.strip() for item in plan.information_needed):
        raise ValueError("Planner requirement names and descriptions must not be blank")
    return plan


def planning_request(question, model, max_tokens):
    """Build Task 1 using the editable prompt file and a structure-only API schema.

    Args:
        question: Nonblank research question inserted into the prompt placeholder.
        model: Anthropic model identifier, without fallback.
        max_tokens: Output token allowance for this planning request.

    Returns:
        Messages API request dictionary; no network call occurs. Missing prompt
        files propagate errors, and blank questions raise ValueError. The original
        approved Task 1 schema is preserved without new model-facing descriptions.
    """
    if not question.strip():
        raise ValueError("Research question must not be blank")
    prompt = (ROOT / "prompts/information_requirements.txt").read_text(encoding="utf-8")
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["information_needed"],
        "properties": {"information_needed": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "name", "description"],
            "properties": {
                "kind": {"type": "string", "enum": ["entity_type", "attribute", "relationship"]},
                "name": {"type": "string"}, "description": {"type": "string"},
            },
        }}},
    }
    return dict(model=model, max_tokens=max_tokens,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt.replace("{research_question}", question)}])
