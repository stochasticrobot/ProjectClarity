"""Tell source-selection tasks what already registered pages have contributed.

A registered source is a URL already admitted to this research run. Before choosing
more URLs, the model receives a compact description of those sources, their
acquisition outcomes and available findings. These helpers add that information
only while the serialized request remains within its character allowance. The
summaries help avoid redundant choices but are not substitutes for quoted evidence.
They use this run's saved state and do not fetch pages or contact the model.
"""
import json
from projectclarity.research_results import result_inventory


def registered_source_context(session):
    """Summarize already registered URLs and their recorded contributions for source selection.

    Include acquisition outcomes and a limited view of findings so another selection
    request can distinguish an unread URL from one already examined. Summaries retain
    pending statuses and do not certify that a page answered the question.

    Args:
        session: Current run with acquired documents, anchored claims and plan goals.
    Returns:
        Per-source URL, read status, goal record counts and bounded examples. Counts
        are exact field tuples, not resolved people. Examples retain pending/support
        labels and at most three 100-character field values; only forty examples
        are included across the payload. Explicit omission flags distinguish a
        sample from all discovered subjects. Unread/error/stale sources never gain
        invented contents. No other run, knowledge store or source body is read.
    """
    from projectclarity.extraction.content_identity import content_aliases
    aliases = content_aliases(session)
    docs = {d.source_id: d for d in session.documents}
    extraction = session.extraction or {}
    rows, example_budget = [], 40
    for source in session.sources:
        if source.kind != 'url':
            continue
        doc = docs.get(source.source_id)
        attempts = [a for a in extraction.get('attempts', []) if a['source_id'] == source.source_id
            and doc and a['text_sha256'] == doc.text_sha256]
        claims = [c for c in extraction.get('candidates', []) if c['source_id'] == source.source_id
            and doc and doc.status == 'retrieved' and c.get('text_sha256') == doc.text_sha256
            and c.get('evidence_status') == 'anchored' and c.get('evidence')]
        row = {'source_id': source.source_id, 'url': source.url,
            'shared_extraction_source_id': aliases.get(source.source_id),
            'retrieval_status': doc.status if doc else 'not_retrieved',
            'completed_passages': sum(a['status'] == 'completed' for a in attempts),
            'unread_passages': sum(a['status'] == 'not_attempted' for a in attempts),
            'passage_inventory_available': bool(attempts), 'goals': []}
        for goal in session.plan.goals:
            matching = [c for c in claims if goal.goal_id in c['proposal'].get('goal_ids', [])]
            if not matching:
                continue
            summary = {'goal_id': goal.goal_id, 'anchored_claims': len(matching)}
            if goal.result_shape:
                inventory = result_inventory(goal.model_dump(), matching)
                summary.update({k: inventory[k] for k in ('unit', 'count_basis', 'supported',
                    'pending', 'disputed', 'incomplete', 'invalid')})
                examples = inventory['records'][:min(2, example_budget)]
                summary['examples'] = [{'status': record['status'],
                    'fields': {k: v[:100] for k, v in list(record['values'].items())[:3]},
                    'fields_truncated': len(record['values']) > 3 or any(len(v) > 100 for v in record['values'].values())}
                    for record in examples]
                summary['examples_omitted'] = len(inventory['records']) - len(examples)
                example_budget -= len(examples)
            row['goals'].append(summary)
        rows.append(row)
    return rows


def add_registered_context(session, payload, system_prompt):
    """Add summaries of known sources while keeping the serialized model request within its limit.

    Registered URLs remain represented even when there is insufficient room for their
    full summaries. This provides selection context without silently making the
    request larger than the adapter can accept.

    Args:
        session: Current run, including prior selection count for rotating summaries.
        payload: Mutable model task data, populated before this function is called.
        system_prompt: Unchanged user-owned text used only for input-size accounting.
    Returns:
        None. Keep every original source ID/URL, add at most 8,000 serialized
        characters of source details, and report the number omitted. Rotate the
        first detailed source across calls to avoid a permanent prefix. If the
        base payload itself exceeds the provider limit, leave normal input rejection
        intact; never truncate questions, evidence or silently remove source URLs.
    """
    from projectclarity.providers.structured_model_call_execution import MAX_MESSAGE_CHARACTERS
    details = registered_source_context(session)
    rows = [{k: row[k] for k in ('source_id', 'url')} for row in details]
    payload['registered_sources'] = rows
    payload['registered_source_summaries_omitted'] = len(rows)

    def size():
        """Measure the same nested JSON encoding used at the provider boundary.

        Args:
            None; reads the current payload and unchanged prompt in this closure.
        Returns:
            Serialized character count; no model or filesystem access occurs.
        """
        return len(json.dumps([('system', system_prompt),
            ('human', json.dumps(payload, ensure_ascii=False))], ensure_ascii=False))

    limit = min(MAX_MESSAGE_CHARACTERS, size() + 8000)
    start = len(session.search.get('selections', [])) % max(1, len(rows))
    for index in [*range(start, len(rows)), *range(start)]:
        previous = rows[index]
        rows[index] = details[index]
        payload['registered_source_summaries_omitted'] -= 1
        if size() > limit:
            rows[index] = previous
            payload['registered_source_summaries_omitted'] += 1
