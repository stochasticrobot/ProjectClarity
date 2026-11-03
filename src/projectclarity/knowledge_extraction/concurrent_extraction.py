"""Assess independent document chunks concurrently without competing state writers.

Each worker owns one chunk and its optional repair. Workers report progress through
an inbox; the document coordinator alone updates session state and the knowledge
index. A worker never starts more than one model call at once, so the worker limit
also bounds extraction and repair calls together. No new calls are retried here.
"""
from projectclarity.knowledge_extraction.knowledge_extraction import extraction_request
from projectclarity.knowledge_extraction.fact_validation_and_repair import recover
from projectclarity.model_access.model_ledger import mark_validation


def extract_chunk(source, chunk, content, requirements, caller, sid, stop, inbox):
    """Return a private chunk receipt while sending operational updates to the owner.

    Args:
        source: Read-only source identity and digest.
        chunk: Read-only chunk offsets, pages and index.
        content: Complete text within those chunk boundaries.
        requirements: Saved research fields passed unchanged to extraction.
        caller: Thread-safe model transport with unique per-chunk stage labels.
        sid: Owning session identifier.
        stop: Shared cancellation event; prevents new extraction/repair calls.
        inbox: Thread-safe queue for call and repair progress, never session writes.
    Returns:
        Receipt containing accepted facts or a recorded failure; None if stopped
        before initial dispatch. Completed responses are retained after a stop,
        but optional repairs will not start. No source/chunk input is mutated.
    """
    if stop.is_set():
        return None
    request = extraction_request(content, requirements, caller.model, 8192)
    receipt = {'status': 'failed', 'request': request, 'source': source['final_url'],
               'document_id': source['id'], 'source_sha256': source['text_sha256'],
               'start_char': chunk['start'], 'end_char': chunk['end'],
               'chunk_index': chunk['index'], 'pages': chunk.get('pages', [])}
    label = f'extract-doc{source["id"]}-chunk{chunk["index"]}'

    def call(stage, payload, session_id):
        """Count actual dispatches and check cancellation at each call boundary.

        Args: stage: Unique task label; payload: Exact request; session_id: Owner.
        Returns: Model text; cancellation or transport errors propagate to recovery
            or the outer receipt handler. Queue notifications precede dispatch.
        """
        if stop.is_set():
            raise InterruptedError('Stopped before model dispatch.')
        inbox.put(('call', source['id'], chunk['index'], None))
        return caller(stage, payload, session_id)

    # Recovery uses the same bounded worker slot and the transport's ledger hooks.
    call.model = caller.model
    if hasattr(caller, 'mark_validation'):
        call.mark_validation = caller.mark_validation

    def progress(accepted, pending):
        """Send observed provisional counts without mutating shared run state.

        Args: accepted: Checked fact count; pending: Proposals awaiting repair.
        Returns: None; queues one update consumed by the coordinator.
        """
        inbox.put(('repair', source['id'], chunk['index'], (accepted, pending)))

    try:
        raw = call(label, request, sid)
        receipt['raw_text'] = raw
        result = recover(raw, content, call, sid,
                         f'extract-repair-doc{source["id"]}-chunk{chunk["index"]}', stop.is_set, progress)
        receipt.update(status='checks_partial' if result['unresolved'] else 'checks_pass', **result)
        for match in receipt['grounding']:
            match['document_start'] = chunk['start'] + match['start']
            match['document_end'] = chunk['start'] + match['end']
        mark_validation(caller, label, sid, not result['initial_rejections'] and not result['entity_issues'])
    except InterruptedError:
        if 'raw_text' not in receipt:
            return None
        raise
    except Exception as error:
        mark_validation(caller, label, sid, False)
        receipt['error'] = {'type': type(error).__name__, 'message': str(error)[:1000]}
    return receipt
