"""Define model replies for broad, source-linked knowledge capture.

Unlike answer-focused extraction, this optional task can record entities,
relationships and attributes even when their relevance to the current question
is unknown. The Pydantic models specify the proposed observations and quotation
references. Later Python checks locate the quotations before storage. Structural
validation is not a factual review. Class docstrings and field descriptions here
are included in the JSON schema sent to the model.
"""
from typing import Literal
from projectclarity.research_schemas import Contract, LocalReference, Text


class EntityMention(Contract):
    """A named entity explicitly mentioned in the supplied source window."""

    entity_id: LocalReference
    name: Text
    kind: Literal["person", "organization", "location", "creation", "event", "concept", "other"]
    quote: Text


class KnowledgeAssertion(Contract):
    """One source-supported relationship or attribute, not an established truth."""

    subject_id: LocalReference
    predicate: Text
    object_id: LocalReference | None
    value: Text | int | float | bool | None
    # Role is part of this relationship's meaning, not an unrelated person's attribute.
    role: Text | None
    quote: Text


class KnowledgeBatch(Contract):
    """Named entities and atomic assertions from a bounded window; report any omitted work."""

    entities: list[EntityMention]
    assertions: list[KnowledgeAssertion]
    # True requests another pass on this same window with prior output provided.
    remaining: bool
