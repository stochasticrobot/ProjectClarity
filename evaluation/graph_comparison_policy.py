"""Normalize declared representation variants without changing observed graph records.

Policies belong to the evaluation case, never a particular model. They express
known equivalence, not fuzzy similarity: unknown labels remain unmatched. Roles,
directions, dates and values retain meaning; the grader still checks source evidence.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from typing import Literal

from pydantic import Field, model_validator
from projectclarity.research_schemas import Contract, Identifier


def label(value):
    """Normalize case and layout without guessing semantic synonyms.

    Args:
        value: A scalar role, name or predicate label; None represents an absent role.

    Returns:
        Case-folded text with collapsed whitespace, or an empty string for None.
    """
    return '' if value is None else ' '.join(str(value).casefold().split())


def predicate_label(value):
    """Treat spaces, hyphens and underscores as predicate-label presentation differences.

    Args:
        value: Expected or observed predicate spelling.

    Returns:
        A normalized token suitable for exact lookup in declared alias tables.
    """
    return label(value).replace('-', '_').replace(' ', '_')


class RoleEquivalence(Contract):
    """One predicate-specific group of interchangeable role representations."""

    predicate: str
    canonical_role: str | None
    accepted_roles: list[str | None] = Field(min_length=1)


class ComparisonPolicy(Contract):
    """Versioned, model-independent acceptance rules for one graph fixture."""

    policy_id: Identifier
    version: Literal[2]
    predicate_aliases: dict[str, list[str]]
    inverse_predicates: dict[str, str]
    role_equivalences: list[RoleEquivalence]
    attribute_formats: dict[str, Literal['year', 'date']]
    date_formats: list[Literal['%Y-%m-%d', '%B %d, %Y', '%d %B %Y']] = Field(min_length=1)

    @model_validator(mode='after')
    def check_unambiguous(self):
        """Reject overlapping policy rules so one spelling cannot have competing meanings.

        Args:
            self: Parsed policy being checked before it can affect a score.

        Returns:
            This policy when alias, inverse and role groups are consistent; otherwise raise
            ValueError. A bad policy cannot silently choose the first matching rule.
        """
        meanings = {}
        canonical = {predicate_label(name) for name in self.predicate_aliases}
        if len(canonical) != len(self.predicate_aliases):
            raise ValueError('Canonical predicate labels collide')
        for name, aliases in self.predicate_aliases.items():
            for alias in [name, *aliases]:
                token, meaning = predicate_label(alias), (predicate_label(name), False)
                if token in meanings and meanings[token] != meaning:
                    raise ValueError('Ambiguous predicate alias')
                meanings[token] = meaning
        for name, target in self.inverse_predicates.items():
            token = predicate_label(name)
            if predicate_label(target) not in canonical or token in meanings:
                raise ValueError('Ambiguous or unknown inverse predicate')
            meanings[token] = (predicate_label(target), True)
        roles = {}
        for group in self.role_equivalences:
            predicate = predicate_label(group.predicate)
            if predicate not in canonical or label(group.canonical_role) not in {label(r) for r in group.accepted_roles}:
                raise ValueError('Role group must name a canonical predicate and include its canonical role')
            for role in group.accepted_roles:
                key = predicate, label(role)
                if key in roles and roles[key] != label(group.canonical_role):
                    raise ValueError('Ambiguous role equivalence')
                roles[key] = label(group.canonical_role)
        if any(predicate_label(name) not in canonical for name in self.attribute_formats):
            raise ValueError('Unknown attribute predicate')
        return self


def read_policy(path):
    """Load an explicit policy while rejecting unknown fields and conflicting equivalences.

    Args:
        path: Local comparison-policy JSON path, not a model-generated instruction.

    Returns:
        A validated ComparisonPolicy; malformed or ambiguous policy files raise an input
        error rather than widening acceptance or making any provider call.
    """
    return ComparisonPolicy.model_validate(json.loads(path.read_text(encoding='utf-8')))


def canonical_value(value, predicate, policy):
    """Normalize only declared year/date representations, preserving incorrect or ambiguous values.

    Args:
        value: Raw attribute value returned by the model or stored in the reference.
        predicate: Canonical predicate token identifying the attribute.
        policy: Validated allowed formats for this case.

    Returns:
        Normalized scalar text or a calendar date/year. Invalid dates, fractional years,
        booleans and unsupported formats remain unmatched; no date order is guessed.
    """
    kind = {predicate_label(k): v for k, v in policy.attribute_formats.items()}.get(predicate)
    if kind == 'year' and not isinstance(value, bool):
        try:
            number = Decimal(str(value))
            if number.is_finite() and number == number.to_integral_value() and 1 <= number <= 9999:
                return str(int(number))
        except InvalidOperation:
            pass
    if kind == 'date' and isinstance(value, str):
        for format_string in policy.date_formats:
            try:
                return datetime.strptime(value.strip(), format_string).date().isoformat()
            except ValueError:
                continue
    return label(value)


def comparison_key(record, kind, policy):
    """Build a semantic comparison key using only the case's declared equivalences.

    Args:
        record: Labelled node, relationship or attribute; raw record is never modified.
        kind: nodes, relationships or attributes.
        policy: Validated versioned rules shared across all model evaluations of this case.

    Returns:
        A hashable identity tuple with canonical predicate, declared inverse orientation,
        predicate-specific role equivalence or attribute value format. An inverse label
        on an attribute has no declared meaning and is not converted.
    """
    if kind == 'nodes':
        return label(record['name']), label(record['kind'])
    token = predicate_label(record['predicate'])
    aliases = {predicate_label(alias): predicate_label(name)
        for name, spellings in policy.predicate_aliases.items() for alias in [name, *spellings]}
    inverses = {predicate_label(name): predicate_label(target) for name, target in policy.inverse_predicates.items()}
    predicate = aliases.get(token, token)
    subject = label(record['subject'])
    if kind == 'attributes':
        return subject, predicate, canonical_value(record['value'], predicate, policy)
    target, role = label(record['object']), label(record.get('role'))
    if token in inverses:
        subject, target, predicate = target, subject, inverses[token]
    for group in policy.role_equivalences:
        if predicate == predicate_label(group.predicate) and role in {label(r) for r in group.accepted_roles}:
            role = label(group.canonical_role)
            break
    return subject, predicate, target, role
