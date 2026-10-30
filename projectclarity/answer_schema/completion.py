"""Read the saved answer requirements used to assess whether research can stop.

When a run has a designed answer, its saved specification defines the completion
checks. These helpers validate those checks, convert them to the format consumed
by evidence assessment, and report entries with unresolved fields or evidence.
For example, finding a person's name does not complete an entry that also requires
a supported affiliation. Runs without explicit designed-answer checks retain the
criteria in their research plan. No model calls, file reads or writes occur here;
returning no field blockers does not itself establish that research is complete.
"""
from projectclarity.answer_schema.contracts import AnswerSchema
from projectclarity.research_schemas import CompletionCriterion


def contract_checks(session):
    """Load the explicit evidence requirements from the saved answer specification.

    An answer specification describes what the final report must contain. These checks
    let assessment and stopping use those requirements rather than a later model
    silently redefining completion. Older specifications without checks return none.

    Args:
        session: Run containing an optional accepted answer schema.
    Returns:
        Explicit typed checks or an empty list. Invalid saved contracts raise
        ValueError rather than yielding an easier completion policy. No mutation.
    """
    if not (session.answer_schema or {}).get('completion_checks'):
        return []
    return AnswerSchema.model_validate(session.answer_schema).completion_checks


def completion_criteria(session):
    """Convert saved answer requirements into the objects consumed by coverage validation.

    Use the answer specification when it has explicit checks; otherwise retain the
    research plan's criteria for compatibility with runs created without those checks.

    Args:
        session: Planned run with optional explicit answer-completion requirements.
    Returns:
        Criterion objects projected from the contract, or the legacy plan criteria.
        Additional unit/evidence guidance remains in the original answer schema.
    """
    checks = contract_checks(session)
    return [CompletionCriterion.model_validate(c.model_dump(
        exclude={'unit', 'result_goal_id', 'evidence_required'})) for c in checks] if checks else list(session.plan.completion_criteria)


def report_blockers(session, ledgers):
    """List report sections whose entries still have unresolved fields or evidence.

    For a designed answer with explicit completion checks, a counted entry with missing
    information must not be overlooked merely because the requested number of entries
    was found. This helper reports those structural blockers; it does not judge whether
    a narrative answer is sufficient or whether every possible item was discovered.

    Args:
        session: Run with an explicit answer contract or legacy report schema.
        ledgers: Complete source-checked result inventories keyed by goal identifier.
    Returns:
        Human-readable blockers for missing inventories or unresolved required
        fields/identities on counted entries. Unassigned observations remain available
        to semantic coverage checks, not automatic field-completeness blockers. Narrative sections rely on their semantic checks and
        acquire no row-count requirement. Empty populations need evidence checks,
        not an invented requirement to find at least one row.
    """
    if not contract_checks(session):
        return []
    blockers = []
    for section in session.answer_schema['sections']:
        if not section['fields']:
            continue
        ledger = ledgers.get(section['goal_id'])
        if ledger is None:
            blockers.append(f"{section['title']}: report fields have not been assessed.")
            continue
        unresolved = sum(ledger.get(k, 0) for k in ('pending', 'disputed', 'identity_pending', 'incomplete', 'invalid'))
        if unresolved:
            blockers.append(f"{section['title']}: {unresolved} report entries have unresolved fields, identity or evidence.")
    return blockers
