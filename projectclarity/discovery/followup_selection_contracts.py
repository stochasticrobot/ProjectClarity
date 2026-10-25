"""Define a model's bounded proposal for investigation, not its permission to fetch.

Only link IDs from Python's observed inventory may be selected. Reasons explain
how a page might help a goal; they do not claim the page has already been read.
Python validates each proposed choice independently before registering its URL.
"""
from pydantic import Field
from projectclarity.research_schemas import Contract, Identifier, Text


class FollowupChoice(Contract):
    """One observed link worth investigating for existing research goals."""
    link_id: Identifier
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    reason: Text


class FollowupPlan(Contract):
    """Explain evidence gaps and rank useful links from the supplied inventory."""
    gaps: list[Text]
    selections: list[FollowupChoice]
    stop_reason: Text = Field(description="Why this selection is useful or empty; not a decision to stop the overall research.")
