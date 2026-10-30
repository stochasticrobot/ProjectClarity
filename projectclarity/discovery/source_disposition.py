"""Validate why a proposed source is relevant to the requested answer.

A source-selection reply can label a page as direct evidence, a specific lead to
investigate, or a rejection. For runs using explicit answer requirements, actionable
choices must name known requirements and compatible research goals. For example,
a directory may be a useful lead even before it supplies a supported researcher
entry. These checks validate the links in the reply, not the page's contents or
semantic relevance. SourceDisposition is a model-facing response schema; its
class and field descriptions are part of the instructions supplied to the model.
"""
from typing import Literal
from pydantic import Field
from projectclarity.research_schemas import Contract, Identifier


class SourceDisposition(Contract):
    """A proposed source's relationship to accepted answer requirements."""

    disposition: Literal['direct_evidence', 'specific_lead', 'reject']
    requirement_ids: list[Identifier] = Field(max_length=24)


def disposition_error(session, choice):
    """Check explicit action scope without interpreting free-text rationale.

    Args:
        session: Planned run with accepted requirements and work mappings.
        choice: Parsed source choice, possibly from the legacy response schema.
    Returns:
        A rejection reason for an actionable choice lacking valid requirement/goal
        links, otherwise None. Explicit rejection is handled separately by the
        shared acceptance boundary. No semantic relevance or page contents are
        inferred. Legacy choices retain their existing structural checks only in
        legacy sessions; a versioned session cannot bypass this boundary by passing
        an old choice object from another caller.
    """
    if not hasattr(choice, 'disposition'):
        from projectclarity.nodes.schedule_work.answer_controller import enabled
        return 'missing_disposition' if enabled(session) else None
    if choice.disposition == 'reject':
        return None
    from projectclarity.answer_schema.completion import contract_checks
    from projectclarity.answer_schema.requirements import requirement_goals
    checks = {c.criterion_id: c for c in contract_checks(session)}
    if not choice.requirement_ids or set(choice.requirement_ids) - checks.keys():
        return 'unknown_or_missing_requirement'
    if any(not set(choice.goal_ids) & requirement_goals(session, checks[rid])
           for rid in choice.requirement_ids):
        return 'requirement_goal_mismatch'
    return None
