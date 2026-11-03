"""Rate one independent link-text batch without changing shared ranking state."""
from projectclarity.model_access.model_client import request_for
from projectclarity.model_access.model_ledger import checked_call
from projectclarity.link_ranking.link_scoring import LinkRatings, validate_ratings


def rank_batch(owner, index, batch, inbox, cached=None):
    """Return checked ratings or a failure receipt for an unchanged batch.

    Args:
        owner: Ranking stage providing read-only question, model and attempt path.
        index: Stable one-based batch number.
        batch: Copied shortlisted link records; never modified by this worker.
        inbox: Queue notifying the coordinator of actual model dispatches.
        cached: Optional exact-request prior successful response and provenance.
    Returns:
        Private receipt with validated ratings, failed status, or None if stopped
        before dispatch. Cached replies are revalidated; no source is fetched and
        no automatic retries occur. Shared counters and output files are untouched.
    """
    if owner.stop.is_set():
        return None
    receipt = {'status':'failed','ids':[row['id'] for row in batch],'attempted_calls':0}
    try:
        data = {'research_question':owner.state['research_question'],'links':[
            {key:row[key] for key in ('id','source_title','link_title','url')} for row in batch]}
        request = request_for('rank_link_titles.txt',data,LinkRatings,owner.caller.model)
        receipt['request'] = request
        if cached is not None and (cached.get('request') != request or cached.get('ids') != receipt['ids']):
            cached = None
        if cached is not None:
            receipt.update(raw_text=cached['raw_text'],reused_from=cached['_reused_from'])
            result = validate_ratings(cached['raw_text'],batch)
        else:
            if owner.stop.is_set():
                return None
            inbox.put(True); receipt['attempted_calls'] = 1

            def validate(text):
                """Retain the provider reply before enforcing exact ID coverage.

                Args: text: Raw model JSON for this batch.
                Returns: Validated ratings; failures keep raw text in the receipt.
                """
                receipt['raw_text'] = text
                return validate_ratings(text,batch)

            result = checked_call(owner.caller,f'link-ranking-{owner.root.name}-{index}',
                request,owner.state['session_id'],validate)
        receipt.update(status='checks_pass',ratings=result.model_dump()['ratings'])
    except Exception as error:
        receipt['error'] = {'type':type(error).__name__,'message':str(error)[:500]}
    return receipt
