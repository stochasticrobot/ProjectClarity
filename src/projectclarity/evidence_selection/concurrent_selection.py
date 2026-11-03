"""Evaluate an isolated selection batch without writing shared session state.

Source checks, prompts and relevance validation are unchanged. The coordinator
owns progress and ordered output; this worker returns a receipt for one batch.
"""
import hashlib
import json
from projectclarity.model_access.model_ledger import mark_validation
from projectclarity.evidence_selection.relevance_selection import prepare_knowledge, selection_request, validate_selection, relevant_knowledge


def select_batch(batch, source_root, question, requirements, caller, sid, attempt_name, stop, inbox, cached=None):
    """Check one original artifact and select relevant fact IDs independently.

    Args:
        batch: Read-only saved artifact reference, count and stable batch ID.
        source_root: Extraction folder restricting allowable input paths.
        question: Final research question shared by every batch.
        requirements: Final answer fields, unchanged between batches.
        caller: Concurrent-safe model transport using unique batch labels.
        sid: Owning session ID.
        attempt_name: Selection attempt name for unique ledger labels.
        stop: Cancellation event checked before any provider dispatch.
        inbox: Thread-safe queue receiving one notification per model dispatch.
        cached: Optional successful prior receipt; exact input/request matches are
            revalidated before reuse, otherwise this batch fails without replay.
    Returns:
        Successful or failed private receipt; None if cancelled before dispatch.
        Empty batches are validated without model calls. Source files and shared
        state are never mutated. No provider retries or prompt changes occur.
    """
    if stop.is_set():
        return None
    receipt = {'status': 'failed', 'source_artifact': {k: batch[k] for k in ('artifact', 'sha256', 'source_url', 'document_id', 'chunk_index')}, 'attempted_calls': 0}
    try:
        path = (source_root / batch['artifact']).resolve()
        if not path.is_relative_to(source_root.resolve()):
            raise ValueError('Artifact is outside the extraction run.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != batch['sha256']:
            raise ValueError('Extraction artifact hash differs from the saved index.')
        knowledge = prepare_knowledge(json.loads(raw), batch['sha256'])
        if len(knowledge['facts']) != batch['facts']:
            raise ValueError('Extraction fact count differs from the saved index.')
        request = selection_request(question, requirements, knowledge, caller.model, 8192)
        receipt['source_artifact']['pages'] = json.loads(raw).get('pages', [])
        receipt['request'] = request
        if cached is not None:
            if cached.get('request') != request or cached.get('source_artifact') != receipt['source_artifact']:
                raise ValueError('Saved selection receipt no longer matches its request/source.')
            text = cached['raw_text']
            receipt['reused_from'] = cached['_reused_from']
        elif knowledge['facts']:
            if stop.is_set():
                return None
            inbox.put(True)
            receipt['attempted_calls'] = 1
            text = caller(f'evidence-{attempt_name}-batch{batch["id"]}', request, sid)
        else:
            text = '{"selected_facts": []}'
        receipt['raw_text'] = text
        selected = validate_selection(text, knowledge)
        store = relevant_knowledge(selected, knowledge)
        receipt.update(status='checks_pass', parsed=selected.model_dump(), relevant_knowledge=store)
        mark_validation(caller,f'evidence-{attempt_name}-batch{batch["id"]}',sid,True)
    except Exception as error:
        mark_validation(caller, f'evidence-{attempt_name}-batch{batch["id"]}', sid, False)
        receipt['error'] = {'type': type(error).__name__, 'message': 'Selection failed validation or its model request. This batch remains unreviewed.'}
    return receipt
