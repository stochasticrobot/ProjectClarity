"""Define a model's bounded proposal for investigation, not its permission to fetch.

Only link IDs from Python's observed inventory may be selected. Reasons explain
how a page might help a goal; they do not claim the page has already been read.
Python validates the entire proposal before registering any new URL.
"""
from pydantic import Field
from projectclarity.research_schemas import Contract, Identifier, Text


class FollowupChoice(Contract):
    """One observed link worth investigating for existing research goals."""
    link_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    reason: Text


class FollowupPlan(Contract):
    """Explain evidence gaps and choose zero to three links from the supplied inventory."""
    gaps: list[Text] = Field(max_length=8)
    selections: list[FollowupChoice] = Field(max_length=3)
    stop_reason: Text
