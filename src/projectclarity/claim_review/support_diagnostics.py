"""Shared descriptive support-gap labels, not new acceptance or recovery rules.

Empty labels on historical records mean unclassified, not proven support.
The reason field in each stage retains the specific missing detail. Categories
describe model judgments; they do not establish that those judgments are correct.
"""
from typing import Literal
from pydantic import Field

FailureCategory = Literal[
    'ambiguous_location', 'ambiguous_name', 'ambiguous_date',
    'ambiguous_reference', 'ambiguous_relationship', 'ambiguous_scope',
    'insufficient_evidence', 'conflicting_evidence', 'unrelated_entity',
    'other_support_issue',
]


def failure_categories_field():
    """Provide independent legacy-compatible category lists for stage responses.

    Args: None.
    Returns: Pydantic field defaulting to an empty list for historical replies.
        New prompts request explicit labels. This field does not alter decisions.
    """
    return Field(default_factory=list, description=(
        'Support problems remaining in this decision; multiple categories may apply. '
        'Use an empty list when no support problem remains.'))
