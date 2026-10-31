"""Enforce and receipt task-specific model inputs before credentials or dispatch.

The extraction node and structured executor share these checks. Limits describe
serialized application messages plus the response schema, not provider tokens.
Exact messages remain in private call traces; this small receipt explains their
components without repeating their text into session logs.
"""
import json
from pathlib import Path

from projectclarity.extraction.extraction_call_tracing import write_private_text

COMMON = {'original_question', 'research_date', 'working_assumptions', 'human_guidance',
    'human_note', 'answer_schema', 'goals', 'scope_in', 'scope_out'}
RECORD_KEYS = {
    'extract_knowledge': COMMON | {'objective', 'source_id', 'source_context', 'passage', 'refinement'},
    'review_claims': COMMON | {'report_requirements', 'source_id', 'source_context', 'passage',
        'entities', 'candidates', 'passages'},
}
TREE_KEYS = {'stage', 'context', 'inputs', 'sources', 'correction'}
FINAL_KEYS = COMMON | {'assessment_policy', 'criteria', 'result_inventory', 'candidates',
    'sources', 'evidence_batches', 'evidence_registry', 'candidate_total', 'intact_anchored_total',
    'offered_total', 'inventory_truncated', 'context_inventory', 'candidate_status_counts',
    'search_allowed', 'acquired_work', 'current_scope_estimates', 'bounded_manifest',
    'previous_searches', 'search_outcomes', 'work_counts', 'search_history_counts'}


def input_receipt(task, messages, schema, *, extraction_settings=None):
    """Measure a task input and detect unexpected fields or oversized envelopes.

    Args:
        task: Application task identifier, never a model-provided routing choice.
        messages: Actual ordered role/text messages about to be sent.
        schema: Pydantic response schema class, included in the measured envelope.
        extraction_settings: Optional resolved extraction bounds; ignored for other
            tasks. None preserves the historical envelope-only preflight.
    Returns:
        JSON receipt with component sizes, contract, limit and explicit errors.
        Unknown tasks retain their existing executor policy and return None.
        Inputs are not truncated or changed; malformed JSON yields a rejection.
    """
    if task not in {*RECORD_KEYS, 'bounded_evidence', 'bounded_coverage'}:
        return None
    limit = 128_000 if task == 'bounded_evidence' else 64_000
    if task == 'extract_knowledge' and extraction_settings is not None:
        limit = extraction_settings.max_input_characters
    message_size = len(json.dumps(messages, ensure_ascii=False))
    schema_size = len(json.dumps(schema.model_json_schema(), ensure_ascii=False))
    errors, payload = [], {}
    try:
        payload = json.loads(messages[-1][1])
        if not isinstance(payload, dict):
            raise ValueError('object required')
    except (ValueError, TypeError, IndexError):
        errors.append('input_contract_invalid_json')
        payload = {}
    allowed = RECORD_KEYS.get(task, TREE_KEYS if task == 'bounded_evidence' else FINAL_KEYS)
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        errors.append('input_contract_unknown_fields')
    if task == 'bounded_coverage':
        inventory = payload.get('result_inventory', {})
        if not isinstance(inventory, dict) or any(not isinstance(ledger, dict)
                for ledger in inventory.values()):
            errors.append('input_contract_invalid_inventory')
        elif any('items' in ledger or 'records' in ledger for ledger in inventory.values()):
            errors.append('input_contract_full_inventory_forbidden')
    if task == 'extract_knowledge' and extraction_settings is not None:
        if message_size > extraction_settings.max_message_characters:
            errors.append('extraction_input_too_large')
    if message_size + schema_size > limit:
        errors.append('input_contract_too_large')
    return dict(contract='task-input-v1', task=task, message_characters=message_size,
        schema_characters=schema_size, total_characters=message_size + schema_size,
        limit_characters=limit, unexpected_fields=unexpected, errors=errors,
        components={key: len(json.dumps(value, ensure_ascii=False)) for key, value in payload.items()},
        selection='assigned_passage_and_record_definitions' if task in RECORD_KEYS else
            'bounded_tree_inputs' if task == 'bounded_evidence' else
            'bounded_summary_and_inventory_counts' if payload.get('evidence_batches') else
            'bounded_direct_evidence_and_inventory_counts')


def save_input_receipt(directory, call_id, receipt):
    """Persist preflight diagnostics even when no model request is allowed.

    Args:
        directory: Existing owner-controlled run directory.
        call_id: Application-generated identifier; no paths are accepted.
        receipt: Result of input_receipt, or None for an unrelated task.
    Returns:
        None. Atomically writes an owner-only size/field receipt. Invalid paths
        raise ValueError; no credentials or source passages enter this receipt.
    """
    if receipt is None:
        return
    if not call_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in call_id):
        raise ValueError('invalid_input_receipt_id')
    folder = Path(directory) / 'input_receipts'
    if folder.is_symlink():
        raise ValueError('invalid_input_receipt_path')
    folder.mkdir(mode=0o700, exist_ok=True)
    write_private_text(folder / (call_id + '.json'), json.dumps(receipt, indent=2))
