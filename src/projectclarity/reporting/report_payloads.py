"""Prepare smaller report inputs without losing source text or attribution.

Only entities referenced by a fact are needed to interpret that fact. The stored
extraction still retains its entire entity list. Within each request, identical
entity definitions and quotations are shared by reference instead of repeated.
Whole facts stay together; oversized records are reported rather than truncated.
"""
import hashlib
import json

INPUT_TOKEN_LIMIT = 16000
MAX_BATCH_FACTS = 40
MAX_MERGE_ITEMS = 16


def canonical_json(value):
    """Serialize saved request content deterministically for cache identity.

    Args: value: JSON-compatible data with no credentials.
    Returns: Compact Unicode JSON preserving all values and list order.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    """Fingerprint exact content so changed evidence or prompts cannot reuse results.

    Args: value: JSON-compatible evidence, request or response.
    Returns: SHA256 hexadecimal content digest; no files are changed.
    """
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def request_size(request):
    """Measure the full request for offline packing, including prompt and schema.

    Args: request: Complete model request, not only its user message.
    Returns: UTF8 serialized bytes. This is not a measured provider token count.
    """
    return len(canonical_json(request).encode())


def compact_packet(packet, facts):
    """Factor exact entities and evidence into dictionaries present in the same call.

    Args:
        packet: Full research question, requirements and coverage context.
        facts: Whole original report fact projections assigned to this batch.
    Returns:
        Compact input preserving every fact field and all quotations. Entity
        definitions unrelated to a fact's subject/object are omitted from that
        fact's context, not from source artifacts. Missing referenced entities
        raise ValueError instead of sending unexplained identifiers.
    """
    entities, evidence, rows = {}, {}, []
    entity_keys, evidence_keys = {}, {}
    for record in facts:
        wanted = {record['fact'].get(key) for key in ('subject_id', 'object_id')} - {None}
        definitions = [item for item in record.get('entities', []) if item['id'] in wanted]
        if {item['id'] for item in definitions} != wanted:
            raise ValueError('A report fact has an unavailable subject/object definition.')
        entity_refs, evidence_refs = [], []
        for item in definitions:
            fingerprint = digest(item)
            key = entity_keys.setdefault(fingerprint, f'E{len(entity_keys)+1}')
            entities[key] = item
            entity_refs.append(key)
        for item in record['evidence']:
            fingerprint = digest(item)
            key = evidence_keys.setdefault(fingerprint, f'Q{len(evidence_keys)+1}')
            evidence[key] = item
            evidence_refs.append(key)
        rows.append({'id': record['id'], 'fact': record['fact'],
                     'entities': entity_refs, 'evidence': evidence_refs})
    return {**{key: packet[key] for key in ('research_question', 'requirements', 'coverage')},
            'facts': rows, 'entity_definitions': entities, 'evidence_passages': evidence}


def entry_key(entry):
    """Group equal labels for review without asserting cross-source identity.

    Args: entry: Structured finding with a human-readable label.
    Returns: Whitespace/case-normalized label; aliases with different names remain
        separate and the merge model must distinguish homonyms within a group.
    """
    return ' '.join(entry['label'].casefold().split())


def fact_group_key(record):
    """Place facts about similarly named subjects near one another before batching.

    Args: record: Original fact with its chunk-scoped entity definitions.
    Returns: Normalized subject-name sort key and original fact ID. This changes
        processing order only; identical names do not establish shared identity,
        and every original fact is still offered exactly once.
    """
    subject = record['fact'].get('subject_id')
    names = [entity['name'] for entity in record['entities'] if entity['id'] == subject]
    return (' '.join((names[0] if names else '').casefold().split()), record['id'])
