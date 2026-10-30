"""Describe which research goals can contribute to each answer requirement.

A plan can contain supporting questions as well as the questions directly answered
by report sections. Explicit requirement IDs link those supporting questions to
the answer checks they help resolve. For example, reading university directories
may supply evidence for a requirement to establish researchers' affiliations.

Coverage assessment uses these links and per-source work counts to distinguish
missing evidence from documents that have not yet been read. The counts include
acquisition outcomes, passage attempts and claims awaiting review; they do not
summarize unread text or establish completion. These helpers only read the supplied
session and return data; they perform no file or network operations.
"""


def requirement_goals(session, criterion):
    """Identify which plan goals may supply evidence for an accepted completion check.

    Args:
        session: Typed session with a non-null plan and goal requirement_ids lists.
        criterion: Completion check with criterion_id and its directly declared goal_ids.
    Returns:
        Set containing the check's declared goals plus goals explicitly referring to
        its criterion ID. When no auxiliary mappings exist, only the directly declared
        goals remain. This set permits relevance checking; it does not establish that
        any claim is supported, belongs in a report row or satisfies the requirement.
        No state is changed and no model or storage operation is performed."""
    return set(criterion.goal_ids) | {g.goal_id for g in session.plan.goals
        if criterion.criterion_id in g.requirement_ids}


def work_inventory_context(session):
    """Describe what has been acquired and read for each registered source.

    Completion assessment needs to distinguish a document with no findings from a
    document whose passages have not yet been analyzed. This summary supplies source
    metadata, passage statuses and pending-review counts without summarizing unread text.

    Args:
        session: Run with source, extraction and review receipts.
    Returns:
        Compact per-source counts of acquired, unread, attempted and pending-review
        work, with acquisition metadata even for sources that yielded no claims yet.
        No unseen content is summarized and counts never prove sufficiency.
    """
    from projectclarity.extraction.content_identity import content_aliases
    aliases = content_aliases(session)
    from collections import Counter
    from projectclarity.source_context import source_context
    attempts = (session.extraction or {}).get('attempts', [])
    candidates = (session.extraction or {}).get('candidates', [])
    documents = {d.source_id: d for d in session.documents}
    rows = []
    for source in session.sources:
        counts = Counter(a['status'] for a in attempts if a['source_id'] == source.source_id)
        document = documents.get(source.source_id)
        rows.append(dict(source_id=source.source_id, source_context=source_context(session, source.source_id),
            shared_extraction_source_id=aliases.get(source.source_id),
            acquisition_status=document.status if document else 'not_acquired',
            passage_count=sum(counts.values()), unread_passages=counts['not_attempted'],
            passage_status_counts=dict(counts), pending_review=sum(
                c['source_id'] == source.source_id and c.get('adjudication_status', 'pending')
                in {'pending', 'needs_review'} for c in candidates)))
    return rows
