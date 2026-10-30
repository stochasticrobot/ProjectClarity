"""Fit saved source choices into a model request without always showing the first rows.

Source-selection requests have a character limit. A run can contain more observed
links or search results than fit in one request, so these helpers interleave rows
from different sources and favor rows shown less often. Rows that do not fit stay
available for later selection. Message size is measured after the same JSON
serialization used for sending. No model request or source retrieval happens here.
"""
import json
from collections import defaultdict, deque

from projectclarity.providers.structured_model_call_execution import MAX_MESSAGE_CHARACTERS


def payload_messages(system, payload):
    """Serialize a compact payload exactly as it will be sent and budget-checked.

    Args:
        system: Application-owned task instructions.
        payload: JSON-compatible task data, including untrusted source content.

    Returns:
        Role/content pairs accepted by the production structured-call helper.
    """
    return [("system", system), ("human", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))]


def balanced_rows(rows, id_key, source_key, exposures):
    """Interleave sources and prioritize least-seen rows so large pages cannot monopolize context.

    Args:
        rows: Complete eligible summary rows, not a pre-truncated prefix.
        id_key: Row field containing the stable inventory identity.
        source_key: Row field identifying the source/parent for fair interleaving.
        exposures: Mapping from row identity to prior model-presentation count.

    Returns:
        All rows in deterministic, least-exposed source-balanced order. No relevance
        judgment or exclusion is made here; the downstream model selects useful work.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[row[source_key]].append(row)
    queues = []
    for group in groups.values():
        group.sort(key=lambda r: exposures.get(r[id_key], 0))
        queues.append(deque(group))
    ordered = []
    while queues:
        queues.sort(key=lambda q: exposures.get(q[0][id_key], 0))
        next_queues = []
        for queue in queues:
            ordered.append(queue.popleft())
            if queue:
                next_queues.append(queue)
        queues = next_queues
    return ordered


def pack_inventory(system, payload, field, rows, *, id_key, source_key, exposures,
                   max_characters=MAX_MESSAGE_CHARACTERS, task=None):
    """Fit fair inventory rows to the real message limit without discarding deferred work.

    Args:
        system: Task instructions included in the adapter's serialized-message budget.
        payload: Base task dictionary, copied before adding rows and context metadata.
        field: Payload field to receive the offered row list.
        rows: Full eligible inventory summaries.
        id_key: Stable identity field returned in offered/deferred receipts.
        source_key: Parent/source identity used to balance the offered context.
        exposures: Prior presentation counts; this function does not modify them.
        max_characters: Optional smaller context allocation, in serialized characters,
            to reserve room for another inventory. Never exceeds the adapter allowance.
        task: Optional task name selecting the adapter's aggregate coverage ceiling;
            omitted retains the ordinary passage/selection ceiling.

    Returns:
        Updated payload and deferred IDs. Metadata discloses total/offered/deferred
        counts. Raise ValueError if base instructions alone exceed the context budget.
        This bounds serialized characters, not an asserted provider token count.
    """
    packed = {**payload, field: [], "context_inventory": {
        "total": len(rows), "offered": 0, "deferred": len(rows)}}
    def fits():
        """Check the exact message serialization used by the production adapter.

        Args:
            None. Read the enclosing packed payload and system instructions.

        Returns:
            True if the current messages fit the adapter's character allowance.
        """
        from projectclarity.providers.structured_model_call_execution import message_character_limit
        return len(json.dumps(payload_messages(system, packed), ensure_ascii=False)) <= min(max_characters, message_character_limit(task))
    if not fits():
        raise ValueError("inventory_base_context_too_large")
    deferred = []
    for row in balanced_rows(rows, id_key, source_key, exposures):
        packed[field].append(row)
        packed["context_inventory"].update(offered=len(packed[field]), deferred=len(rows) - len(packed[field]))
        if not fits():
            packed[field].pop()
            deferred.append(row[id_key])
            packed["context_inventory"].update(offered=len(packed[field]), deferred=len(rows) - len(packed[field]))
    return packed, deferred
