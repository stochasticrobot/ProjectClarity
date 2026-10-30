"""Create repeatable keys for result records without guessing who a name refers to.

Reporting and coverage need repeated evidence for the same requested item to count
once. These helpers normalize formatting in the supplied identifying fields and
combine them with the goal ID. Explicitly typed person names can have conventional
titles or display order normalized; organization names are protected from that
person-name rule. Nicknames, initials, translations and conflicting attributes are
not assumed equivalent. Callers decide which fields constitute identity; this
module performs no model calls, source reads or cross-run entity reconciliation.
"""
import hashlib
import json
import re
import unicodedata

ACCOUNTING_VERSION = 'goal-results-v2'
NAME_FIELDS = frozenset({'name', 'full_name', 'person_name', 'member_name',
    'researcher_name', 'organization_name', 'organisation_name'})
PERSON_FIELDS = frozenset({'full_name', 'person_name', 'member_name', 'researcher_name'})
PERSON_LABEL_FIELDS = NAME_FIELDS - {'organization_name', 'organisation_name'}


def normalized(value):
    """Normalize spelling layout while preserving meaningful words and punctuation.

    Args:
        value: Observed text field, not an inferred alias.
    Returns:
        NFC, case-folded, whitespace-collapsed text; no state changes occur.
    """
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def person_label(value):
    """Reconcile explicit display order and conventional titles, not fuzzy identity.

    Args:
        value: Text from an explicitly person-named field or typed person entity.
    Returns:
        Normalized name, reversing one explicit Last, First comma and stripping
        leading titles. Initials, accents and differing names remain distinct.
    """
    value = normalized(value)
    if value.count(',') == 1:
        last, first = value.split(',', 1)
        value = first.strip() + ' ' + last.strip()
    return re.sub(r'^(?:(?:the )?(?:right honourable|honourable)|right hon\.|rt\. hon\.|hon\.|dr\.|prof\.|mr\.|mrs\.|ms\.)\s+', '', value)


def result_key(values, person_values=(), organization_values=()):
    """Build a comparison key from every identifying field supplied for one goal.

    Repeated field tuples can group together after formatting normalization. Missing
    or competing values are not filled from other observations.

    Args:
        values: Complete goal-specific field dictionary.
        person_values: Exact normalized labels explicitly typed as people in this run.
        organization_values: Explicit organization labels, which override generic
            full_name/member_name field conventions and are never person-reordered.
    Returns:
        Stable tuple of normalized field/value pairs. Generic organization names
        and arbitrary event/relationship fields are never reordered as people.
    """
    return tuple(sorted((field, person_label(value) if normalized(value) not in organization_values
        and (field.casefold() in PERSON_FIELDS
        or (field.casefold() in PERSON_LABEL_FIELDS and person_label(value) in person_values))
        else normalized(value)) for field, value in values.items()))


def result_id(goal_id, key):
    """Give one goal-local result a stable presentation/reference identifier.

    Args:
        goal_id: Owning research goal; identities never merge across goals.
        key: Canonical result field tuple.
    Returns:
        Short deterministic hash, independent of evidence order or review status.
    """
    return hashlib.sha256(json.dumps([goal_id, key], ensure_ascii=False).encode()).hexdigest()[:24]


def person_labels(extraction):
    """Collect explicitly typed person labels for consistent result normalization.

    Args:
        extraction: This run's serialized extraction receipts, or None.
    Returns:
        Normalized labels typed person/human/individual. No type is inferred from
        the question, benchmark, organizational name or entity mention count.
    """
    return {person_label(e['name']) for a in (extraction or {}).get('attempts', [])
        for e in (a.get('result') or {}).get('entities', [])
        if normalized(e['entity_type']) in {'person', 'human', 'individual'}}


def organization_labels(extraction):
    """Protect explicit organization names from person-format normalization.

    Args:
        extraction: This run's extraction receipts, or None.
    Returns:
        Layout-normalized labels explicitly typed as organizations or businesses.
        This is a formatting guard, not a factual type classification or alias map.
    """
    kinds = {'organization', 'organisation', 'company', 'institution', 'business',
        'corporation', 'nonprofit', 'charity', 'agency', 'political party', 'team', 'group'}
    return {normalized(e['name']) for a in (extraction or {}).get('attempts', [])
        for e in (a.get('result') or {}).get('entities', []) if normalized(e['entity_type']) in kinds}
