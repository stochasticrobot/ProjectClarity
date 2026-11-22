"""Recognize exact catalogue echoes without treating them as new research facts.

Some models return existing entities alongside new ones when asked for a list of
additions. This module removes only exact copies of already-validated entities
from a schema-validated review. It records each removal, does not edit existing
entities, and never resolves conflicting definitions or invents missing records.
This is a task-specific compatibility rule, independent of the model provider.
"""
from copy import deepcopy


def normalize_entity_echoes(review, existing):
    """Convert an echoed catalogue into additions without changing its meaning.

    Args:
        review: Strictly schema-validated review dictionary with entities/decisions.
            It is read-only; required fields and extra-field checks must run first.
        existing: Strictly validated original entity list, retained unchanged.
    Returns:
        Pair of detached review and audit records for exact echoed entities removed
        from its additions list. Conflicting existing definitions and repeated IDs
        within either list raise ValueError. New entities, decisions and all other
        content are untouched for normal validation. No calls, inference or writes.
    """
    known = {entity['id']: entity for entity in existing}
    if len(known) != len(existing):
        raise ValueError('Original entity catalogue contains duplicate IDs.')
    ids = [entity['id'] for entity in review['entities']]
    if len(ids) != len(set(ids)):
        raise ValueError('Review contains duplicate entity proposals.')
    result = deepcopy(review)
    kept, changes = [], []
    for index, entity in enumerate(review['entities']):
        identifier = entity['id']
        if identifier not in known:
            kept.append(deepcopy(entity))
        elif entity != known[identifier]:
            raise ValueError('Review attempts to redefine an existing entity.')
        else:
            changes.append({'path': f'/entities/{index}', 'operation': 'remove_exact_existing_entity_echo',
                            'entity_id': identifier, 'matched_original': deepcopy(known[identifier])})
    result['entities'] = kept
    return result, changes
