"""Investigate one claim using an immutable saved-document evidence snapshot.

Workers own their claim packets and receipts. Only the coordinator updates shared
review rows, report knowledge and progress. Exact saved successful responses can
be revalidated on resume without repeating model calls.
"""
from projectclarity.knowledge_extraction.knowledge_extraction import save
from projectclarity.model_access.model_client import request_for
from projectclarity.model_access.model_ledger import mark_validation


def investigate_claim(owner, collector, row, inbox, cached=None):
    """Collect evidence and assess one claim without mutating shared progress.

    Args:
        owner: InvestigationRun providing read-only question, model and output path.
        collector: Initialized read-only EvidenceCollector shared by workers.
        row: Copied original claim record and stable review ID.
        inbox: Queue for packet-ready and dispatch notifications to the coordinator.
        cached: Optional successful prior receipt with exact-request provenance.
    Returns:
        Receipt containing packet, decision and citations, or interrupted/failed
        status. Saves a unique packet before dispatch; never writes shared stores.
        Stops before new calls, retains in-flight results and never auto-retries.
    """
    from projectclarity.claim_review.claim_investigation import InvestigationDecision, validate_investigation
    receipt = {'status':'failed','attempted_calls':0}
    label = f'investigate-{owner.root.name}-claim{row["id"]}'
    try:
        owner.cancelled()
        packet = collector.collect(row['record'])
        packet.update(research_question=owner.state['research_question'], requested_information=owner.state['information_needed'])
        receipt['packet'] = packet
        save(owner.root/f'packet-{row["id"]:04d}.json',packet,'')
        inbox.put(('packet',row['id'],packet))
        owner.cancelled()
        request = request_for('investigate_claims.txt',packet,InvestigationDecision,owner.caller.model)
        request['system'] = owner.prompt; receipt['request'] = request
        if cached is not None:
            if cached.get('request') != request:
                raise ValueError('Saved investigation request/evidence changed; cannot reuse review.')
            text = cached['raw_text']; receipt['reused_from'] = cached['_reused_from']
        else:
            owner.cancelled()
            receipt['attempted_calls'] = 1; inbox.put(('call',row['id'],None))
            text = owner.caller(label,request,owner.state['session_id'])
        receipt['raw_text'] = text
        decision = validate_investigation(text,packet)
        by_id = {p['passage_id']:p for p in packet['passages']}
        receipt.update(status='checks_pass',assessment=decision.model_dump(),
            supporting_evidence=[{**by_id[c.passage_id],'quote':c.quote} for c in decision.citations])
        mark_validation(owner.caller,label,owner.state['session_id'],True)
    except InterruptedError:
        receipt['status'] = 'interrupted'
    except Exception as error:
        mark_validation(owner.caller,label,owner.state['session_id'],False)
        receipt['error'] = {'type':type(error).__name__,'message':str(error)[:500]}
    return receipt
