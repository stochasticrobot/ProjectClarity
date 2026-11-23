"""Describe the answer format chosen from the user's intent before research begins.

A saved plan names the report type, explains the choice, and identifies the requested
subject for a profile. These are research instructions, not source-backed claims.
The same plan follows the run into searches and report requests; evidence shortages
do not authorize changing the requested format.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict


class ReportPlan(BaseModel):
    """Model-selected format and user-requested identity, not verified entity data."""
    model_config = ConfigDict(extra='forbid', strict=True)
    report_type: Literal['directory', 'entity_profile', 'comparison', 'timeline', 'topic_overview']
    reason: str
    subject: str | None
    subject_kind: Literal['company', 'person', 'organization'] | None
    subject_scope: str | None


def validate_report_plan(plan):
    """Check that a saved format has enough explicit context to guide reporting.

    Args:
        plan: ReportPlan or dictionary returned during clarification/refinement.
    Returns:
        Validated ReportPlan. Profile subject and kind must be supplied; scope may
        be null when the user supplied no identity qualifier, or nonblank text.
        Other formats may retain optional target metadata without becoming profiles.
        Routing depends only on report_type; no supplied scope is discarded. Invalid
        data raises ValueError. No model calls or semantic guarantees are made.
    """
    result = ReportPlan.model_validate(plan)
    if not result.reason.strip():
        raise ValueError('Report choice needs an explanation.')
    if result.report_type == 'entity_profile':
        if not result.subject or not result.subject.strip() or not result.subject_kind:
            raise ValueError('Entity profiles require a named subject and kind.')
        if result.subject_scope is not None and not result.subject_scope.strip():
            raise ValueError('Profile scope must be null or nonblank text.')
    return result


def saved_report_plan(state):
    """Read a stored plan, keeping pre-feature sessions on their original directory path.

    Args:
        state: Saved session dictionary, not browser-supplied replacement settings.
    Returns:
        Validated plan dictionary. Only absence of the key means historical directory
        behavior; explicit null, malformed or unsupported values never become a
        directory by fallback. This helper does not test implementation availability.
    """
    if 'report_plan' not in state:
        return {'report_type': 'directory', 'reason': 'Historical session: original directory reporting.',
                'subject': None, 'subject_kind': None, 'subject_scope': None}
    return validate_report_plan(state['report_plan']).model_dump()
