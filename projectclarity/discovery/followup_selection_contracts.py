"""Define model replies for choosing additional sources and searches.

A source selector receives already observed link or search-result IDs and proposes
which are worth reading for specified research goals. These Pydantic models define
that proposal format, including the reason and required references. A valid model
reply is not permission to fetch an arbitrary URL: selection code still verifies
that the IDs were offered and applies source policies and remaining capacity.
Class and field descriptions become part of the schema sent to the model.
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


from projectclarity.discovery.source_disposition import SourceDisposition


class ContractFollowupChoice(FollowupChoice, SourceDisposition):
    """An observed link with an explicit requirement-linked disposition."""


class ContractFollowupPlan(FollowupPlan):
    """Source decisions for answer-contract runs, including explicit rejection."""

    selections: list[ContractFollowupChoice]
