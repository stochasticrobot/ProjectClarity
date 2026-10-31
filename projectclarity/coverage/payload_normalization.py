"""Share exact repeated qualification data inside oversized coverage requests.

The coverage assessor otherwise repeats requirement wording and source context
inside every report entry. This module replaces sufficiently large, exactly equal
JSON values in those qualification details with local JSON Pointer references.
The referenced values travel in the SAME request; no lookup or model call is
needed to obtain them. Report rows, names, statuses and evidence multiplicities
are not merged. For example, two identical source-context objects can share one
definition, but similar person names never establish a shared identity.

Only the transport projection changes. Original receipts and report inventories
remain untouched. Expansion reconstructs the original JSON data, not necessarily
object-key formatting. This proves data preservation, not that a live model will
interpret references as accurately as inline evidence. Prompt text and response
schemas are outside this module's ownership.
"""
from collections import Counter
from copy import deepcopy
import json

from projectclarity.discovery.inventory_context import payload_messages
from projectclarity.coverage.evidence_batches import fingerprint

ENCODING = 'qualification-json-pointers-v1'
TABLE = 'shared_qualification_values'
FORMAT = 'coverage_payload_encoding'
PREFIX = '#/' + TABLE + '/'
MIN_SHARED_CHARACTERS = 128


def qualification_roots(payload):
    """Locate per-entry qualification trees without treating report rows as duplicates.

    Args:
        payload: Coverage input with optional goal-keyed result_inventory and items.
    Returns:
        Iterator of entry dictionaries containing qualification_requirements.
        No values are changed; callers may operate on their own copied payload.
    """
    for ledger in payload.get('result_inventory', {}).values():
        for row in ledger.get('items', []):
            if 'qualification_requirements' in row:
                yield row


def json_key(value):
    """Compare JSON values exactly without name, Unicode or whitespace normalization.

    Args:
        value: JSON-compatible scalar, list or object from qualification data.
    Returns:
        Canonical JSON retaining array order, scalar types and all string bytes.
        Object key order is not significant. Non-JSON numbers raise ValueError.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def normalize_qualification_payload(payload):
    """Factor repeated qualification values into an in-request reference dictionary.

    Args:
        payload: Original JSON-compatible coverage input; never mutated.
    Returns:
        A copied input, optionally carrying a version tag and shared dictionary.
        Only repeated strings/containers longer than 128 serialized characters
        are eligible. References preserve every occurrence and list position.
        Existing reserved keys or literal reference-shaped objects disable the
        encoding instead of risking reinterpretation. Definitions remain literal,
        never recursive references. No clipping, identity inference or I/O occurs.
    """
    result = deepcopy(payload)
    if TABLE in payload or FORMAT in payload:
        return result
    counts = Counter()
    collision = False

    def count(value):
        """Count exact eligible values and detect ambiguous literal reference objects.

        Args:
            value: One nested JSON value in the original qualification trees.
        Returns:
            None. Updates local counts/collision state, not the supplied value.
        """
        nonlocal collision
        if isinstance(value, dict) and set(value) == {'$ref'}:
            collision = True
        if isinstance(value, (str, dict, list)):
            key = json_key(value)
            if len(key) > MIN_SHARED_CHARACTERS:
                counts[key] += 1
        children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        for child in children:
            count(child)

    for row in qualification_roots(payload):
        count(row['qualification_requirements'])
    if collision:
        return result
    definitions, identifiers = {}, {}

    def replace(value):
        """Replace equal large values, keeping each shared definition literal and local.

        Args:
            value: Qualification subtree from the copied transport projection.
        Returns:
            Fresh subtree or a local reference. Adds definitions on first use;
            does not modify original data or combine separate evidence occurrences.
        """
        if isinstance(value, (str, dict, list)):
            key = json_key(value)
            if counts[key] > 1:
                if key not in identifiers:
                    identifier = 'Q' + str(len(identifiers) + 1)
                    identifiers[key] = identifier
                    definitions[identifier] = deepcopy(value)
                return {'$ref': PREFIX + identifiers[key]}
        if isinstance(value, dict):
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child) for child in value]
        return value

    for row in qualification_roots(result):
        row['qualification_requirements'] = replace(row['qualification_requirements'])
    if definitions:
        result[FORMAT] = ENCODING
        result[TABLE] = definitions
    return result


def expand_qualification_payload(payload):
    """Recover the original coverage JSON for inspection and preservation checks.

    Args:
        payload: Raw coverage input or this module's explicitly tagged projection.
    Returns:
        Fresh expanded input with encoding metadata removed. Unknown encodings,
        missing/nonlocal references and malformed definition tables raise ValueError.
        Shared definitions are copied as literal data, never recursively executed.
        This is an inspection decoder, not an extra step before model invocation.
    """
    result = deepcopy(payload)
    if FORMAT not in result:
        return result
    if result.pop(FORMAT) != ENCODING:
        raise ValueError('unknown_coverage_payload_encoding')
    table = result.pop(TABLE, None)
    if not isinstance(table, dict):
        raise ValueError('invalid_coverage_reference_table')

    def expand(value):
        """Resolve only explicit local markers within qualification details.

        Args:
            value: Encoded qualification subtree, not arbitrary source instructions.
        Returns:
            Independent expanded JSON value. Broken or external pointers fail;
            no URL, path, code or instruction is followed.
        """
        if isinstance(value, dict):
            if set(value) == {'$ref'}:
                ref = value['$ref']
                if not isinstance(ref, str) or not ref.startswith(PREFIX) or ref[len(PREFIX):] not in table:
                    raise ValueError('invalid_coverage_reference')
                return deepcopy(table[ref[len(PREFIX):]])
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    for row in qualification_roots(result):
        row['qualification_requirements'] = expand(row['qualification_requirements'])
    return result


def prepare_coverage_messages(system, payload, max_characters):
    """Measure the real message envelope and use normalization only when beneficial.

    Args:
        system: Verbatim approved coverage prompt, never augmented or rewritten.
        payload: Complete raw direct or aggregate coverage input, not modified.
        max_characters: Existing serialized-message allowance, not a token budget.
    Returns:
        (messages, receipt) for the exact representation the caller should send.
        Small requests stay unchanged. Oversized requests use the reference form
        only if the serialized envelope shrinks and expansion matches the input.
        The receipt records raw/effective sizes and hashes, not provider tokens.
        This does not authorize oversized requests: the caller still enforces its
        limit and existing fallback. Response-schema size is not part of this
        historical message-character limit. No model invocation or persistence.
    """
    messages = payload_messages(system, payload)
    original_size = len(json.dumps(messages, ensure_ascii=False))
    effective, size, wire_payload = messages, original_size, payload
    if original_size > max_characters:
        candidate = normalize_qualification_payload(payload)
        encoded = payload_messages(system, candidate)
        encoded_size = len(json.dumps(encoded, ensure_ascii=False))
        if encoded_size < original_size:
            if json_key(expand_qualification_payload(candidate)) != json_key(payload):
                raise ValueError('coverage_normalization_not_lossless')
            effective, size, wire_payload = encoded, encoded_size, candidate
    receipt = dict(encoding=ENCODING if wire_payload is not payload else 'inline',
        original_characters=original_size, effective_characters=size,
        shared_values=len(wire_payload[TABLE]) if wire_payload is not payload else 0,
        original_payload_sha256=fingerprint(payload),
        wire_payload_sha256=fingerprint(wire_payload))
    return effective, receipt
