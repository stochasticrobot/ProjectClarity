"""Coverage policy identity, exact search deduplication and scheduler compatibility.

All production assessments dispatch to bounded_assessment. The old flat-batch
executor is retired. compact_ledgers remains for offline legacy projection tests,
not for model input; historical prompt loading supports frozen replay evidence.
"""
from projectclarity.coverage.evidence_batches import fingerprint
from projectclarity.coverage.population import POPULATION_POLICY
from projectclarity.coverage.assessment_diagnostics import query_key, query_fingerprint
from projectclarity.nodes.assess_research_gaps.node import SYSTEM_PROMPT
from projectclarity.prompt_loader import load_prompt

# Historical replay compatibility only; no production dispatcher uses this prompt.
DIGEST_PROMPT = load_prompt('summarize_evidence.txt')
MAX_COVERAGE_CHARACTERS = 512_000


def policy_revision():
    """Hash current bounded-assessment instructions and response contract.

    Saved completion judgments must not silently remain current after the model-facing
    assessment policy changes. The returned hash identifies that policy version.

    Args:
        None. Uses verbatim loaded prompts, the typed schema and policy version.
    Returns:
        Stable policy digest including benchmark validation, preventing earlier global judgments from silently
        surviving a changed prompt/schema. No file reads or state mutation.
    """
    from projectclarity.coverage.bounded_contracts import BoundedDigest
    from projectclarity.coverage.bounded_assessment import SYSTEM_PROMPT as BOUNDED_PROMPT
    return fingerprint([BOUNDED_PROMPT, BoundedDigest.model_json_schema(), 'bounded-input-v1', POPULATION_POLICY, SYSTEM_PROMPT])




def compact_ledgers(ledgers):
    """Retain the historical inventory projection for offline accounting checks.

    Keep every report entry, its values and evidence references. Remove redundant detail
    for supported fields while retaining unresolved alternatives, so reducing request
    size does not hide a disagreement or missing field. Production coverage no
    longer calls this helper or sends these row-level inventories to the model.

    Args:
        ledgers: Complete source-checked goal inventories from coverage_results.
    Returns:
        New inventories retaining all rows, values, candidate links, counts and
        unresolved field alternatives. Supported fields already have their values,
        row status and citations; no conflicts or incomplete observations are clipped.
    """
    return {gid: {**ledger, 'items': [{**item, 'field_status': {
        name: field for name, field in (item.get('field_status') or {}).items()
        if field['status'] != 'supported'}} for item in ledger['items']]}
        for gid, ledger in ledgers.items()}




def batch_schedule_key(session):
    """Describe saved evidence-batch progress so the scheduler can identify the next task.

    A change from pending to completed or failed batch work changes the task identity.
    Repeated calls without such progress must not create unlimited fresh retries.

    Args:
        session: Run with optional active batch IDs and saved digest receipts.
    Returns:
        Small stable identity rows. Changed receipt states create the next work
        unit, but aggregate attempts/history do not manufacture unlimited retries.
    """
    receipts = session.coverage.get('evidence_batches', {})
    return [[bid, receipts.get(bid, {}).get('status'), receipts.get(bid, {}).get('attempts', 0)]
        for bid in session.coverage.get('batch_progress', {}).get('batch_ids', [])]




def queue_searches(session, answer, diagnostics=None):
    """Retain valid blocker leads independently of a rejected completion judgment.

    Args:
        session: Mutable run containing permitted goals and prior search receipts.
        answer: Parsed global assessment with proposed follow-up queries.
        diagnostics: Optional history snapshot receiving per-proposal dispositions;
            None preserves callers that do not request diagnostics.
    Returns:
        True if all proposed goal references are known; otherwise False without
        altering the queue. Accepted leads are deduplicated and attempted queries
        excluded. Search permission and budgets remain enforced by the scheduler.
        Diagnostics distinguish queue admission from execution; no fuzzy matching occurs.
    """
    goals = {g.goal_id for g in session.plan.goals}
    decisions = []
    if diagnostics is not None:
        diagnostics['search_decisions'] = decisions
    if any(set(q.goal_ids)-goals for q in answer.next_searches):
        decisions.extend(dict(proposal_index=i, query_sha256=query_fingerprint(q.query),
            disposition='rejected_unknown_goal_batch') for i, q in enumerate(answer.next_searches))
        return False
    if not session.request.web_search:
        decisions.extend(dict(proposal_index=i, query_sha256=query_fingerprint(q.query),
            disposition='search_disabled') for i, q in enumerate(answer.next_searches))
        return True
    attempted = {query_key(q['query']) for q in session.search.get('queries', [])}
    queued = {}
    for index, query in enumerate([*[q.model_dump(mode='json') for q in answer.next_searches],
                  *session.coverage.get('next_searches', [])]):
        key = query_key(query['query'])
        if index < len(answer.next_searches):
            decisions.append(dict(proposal_index=index, query_sha256=query_fingerprint(query['query']),
                disposition='already_attempted' if key in attempted else
                'duplicate_proposal' if key in queued else 'queued_not_executed'))
        if key not in attempted:
            queued.setdefault(key, query)
    session.coverage['next_searches'] = list(queued.values())
    return True




def assess_contract(state, output_root, key_reader, monitor=None):
    """Dispatch every coverage run through the shared bounded production engine.

    Args:
        state: Graph state containing the mutable research session.
        output_root: Saved-run parent directory used for source verification.
        key_reader: Existing credential callback, used only after preflight checks.
        monitor: Optional cancellation and progress observer.
    Returns:
        Session update after one bounded logical call or explicit preflight failure.
        Both designed and legacy contracts retain their completion gates; no route
        falls back to sending the full report inventory.
    """
    from projectclarity.coverage.bounded_assessment import assess
    return assess(state, output_root, key_reader, monitor)
