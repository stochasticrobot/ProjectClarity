"""Repair invalid temporary entity and claim labels in a model extraction reply.

The model gives entities and claims local IDs so its references can be connected
within one reply. A label with invalid formatting or excessive length can sometimes
be replaced consistently without changing the content, for example replacing an
overlong entity label wherever it is referenced. The result must still pass the
unchanged extraction schema. Duplicate or unknown references are not guessed,
and quotations, statements and external identifiers are not rewritten. The caller
keeps the original reply and the record of label replacements.
"""
import copy
import hashlib
import json
import re

from projectclarity.extraction.extraction_contracts import ExtractionBatch


def recover_local_identifiers(response_text: str):
    """Replace recoverable invalid local IDs consistently within one extraction reply.

    Entity and claim labels connect references inside a model response. Repair their
    format only when the association is unambiguous, then revalidate the unchanged
    response schema. Never rewrite claims, quotations or external identifiers.

    Args:
        response_text: Complete JSON reply from a normally finished model call.

    Returns:
        Validated ExtractionBatch and a mapping of changed local labels. Duplicate IDs,
        empty/non-string labels, unknown references or other schema errors raise rather
        than dropping claims. No facts, quotes, goals or passage IDs are rewritten.
    """
    payload = copy.deepcopy(json.loads(response_text))
    aliases = {}
    valid = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
    entities = payload['entities']
    claims = payload['claims']
    entity_aliases = {}
    for records, field, prefix in [(entities, 'entity_id', 'E'), (claims, 'claim_id', 'C')]:
        local_aliases = {}
        labels = [record[field] for record in records]
        if any(not isinstance(label, str) or not label for label in labels) or len(set(labels)) != len(labels):
            raise ValueError('ambiguous_local_identifiers')
        for label in labels:
            if not valid.fullmatch(label):
                replacement = prefix + hashlib.sha256(label.encode()).hexdigest()[:31]
                if replacement in labels or replacement in aliases.values():
                    raise ValueError('local_identifier_collision')
                local_aliases[label] = replacement
                aliases[f'{field}:{label}'] = replacement
        for record in records:
            record[field] = local_aliases.get(record[field], record[field])
        if field == 'entity_id':
            entity_aliases = local_aliases
    original_entities = {record['entity_id'] for record in json.loads(response_text)['entities']}
    for claim in claims:
        references = list(claim['entity_ids'])
        relationship = claim.get('relationship')
        if relationship is not None:
            references += [relationship['subject_id'], relationship['object_id']]
        if any(value not in original_entities for value in references):
            raise ValueError('unknown_entity_reference')
        claim['entity_ids'] = [entity_aliases.get(value, value) for value in claim['entity_ids']]
        if relationship is not None:
            for field in ('subject_id', 'object_id'):
                relationship[field] = entity_aliases.get(relationship[field], relationship[field])
    if not aliases:
        raise ValueError('no_local_identifiers_to_recover')
    return ExtractionBatch.model_validate(payload), aliases
