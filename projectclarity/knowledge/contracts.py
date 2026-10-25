"""Structured observation capture; topic relevance never excludes an entity.

These schemas are sent to the model. Entities remain source-scoped until a later
identity-resolution step. Attributes and relationships carry their own quotations,
so one ambiguous date cannot invalidate unrelated affiliation evidence.
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
