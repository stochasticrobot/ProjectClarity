"""Review independent triage batches without writing shared research state.

Each worker receives one original source group and returns a checked receipt.
Prompts, evidence checks and saved-response recovery remain identical to serial
triage. The owning coordinator alone updates buckets, counters and progress.
"""
import hashlib
import json
from projectclarity.evidence_selection.relevance_selection import prepare_knowledge
from projectclarity.model_access.model_ledger import mark_validation


def review_batch(owner, batch, records, inbox):
    """Check a source group and return decisions or an isolated failure receipt.

    Args:
        owner: TriageRun providing read-only context, model transport and the
            read-only saved-response lookup. Workers do not mutate its state.
        batch: Stable group ID and source reference copied by the coordinator.
        records: Original selected records for this group, never rewritten.
        inbox: Thread-safe dispatch notifications consumed by the coordinator.
    Returns:
        Private success/failure receipt, or None if stopped before work/dispatch.
        Completed model replies are validated after a stop and retained. Exact
        saved replies are revalidated; previously failed batches stay unreviewed
        rather than being retried. No shared state or source files are written.
    """
    from projectclarity.claim_review.claim_triage import Decisions, validate_decisions
    if owner.stop.is_set():
        return None
    receipt = {'status':'failed', 'source':batch['source'], 'attempted_calls':0}
    try:
        source = batch['source']; path = (owner.source_root / source['artifact']).resolve()
        if not path.is_relative_to(owner.source_root.resolve()): raise ValueError('Source outside run.')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['sha256']: raise ValueError('Source hash changed.')
        original = json.loads(raw); knowledge = prepare_knowledge(original,source['sha256'])
        by_id = {r['fact_id']:r for r in knowledge['facts']}
        for record in records:
            if record['fact'] != by_id[record['fact_id']]['fact'] or record['source_fact_index'] != by_id[record['fact_id']]['source_fact_index']:
                raise ValueError('Selected record differs from original extraction.')
        chunk = json.loads(original['request']['messages'][0]['content'])['document_chunk']
        request = {'model':owner.caller.model, 'max_tokens':8192, 'system':owner.prompt,
            'output_config':{'format':{'type':'json_schema', 'schema':Decisions.model_json_schema()}},
            'messages':[{'role':'user','content':json.dumps({'research_question':owner.state['research_question'],
                'requested_information':owner.state['information_needed'], 'document_chunk':chunk,
                'entities':knowledge['entities'], 'facts':records},ensure_ascii=False)}]}
        receipt['request']=request
        recovered=owner.recovered_response(batch['id'],request)
        if recovered:
            text,receipt['recovered_from']=recovered

        else:
            if owner.stop.is_set():
                return None
            receipt['attempted_calls']=1;inbox.put(True)
            text = owner.caller(f'triage-{owner.root.name}-batch{batch["id"]}',request,owner.state['session_id'])
        receipt['raw_text']=text
        decisions = validate_decisions(text,records)
        receipt.update(status='checks_pass',decisions=[d.model_dump() for d in decisions])
        mark_validation(owner.caller,f'triage-{owner.root.name}-batch{batch["id"]}',owner.state['session_id'],True)
    except Exception as error:
        mark_validation(owner.caller,f'triage-{owner.root.name}-batch{batch["id"]}',owner.state['session_id'],False)
        receipt['error']={'type':type(error).__name__,'message':str(error)[:1000]}
    return receipt
