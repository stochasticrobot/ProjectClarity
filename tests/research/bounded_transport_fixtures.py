"""Synthetic accounting-only replies for production workflow transport tests.

These responses exercise bounded orchestration, not summarization accuracy.
Witnesses include the first and last offered IDs to expose prefix starvation.
"""


def digest_reply(payload):
    """Acknowledge all immediate inputs without pretending to interpret source prose.

    Args:
        payload: Actual production leaf or reduction request decoded at HTTP transport.
    Returns:
        Schema-valid synthetic reply preserving child blockers and bounded witnesses.
        It is never a live semantic judgment or a reference research answer.
    """
    rows = payload['inputs']
    pool = [row['candidate_id'] for row in rows] if payload['stage']=='evidence' else [
        cid for row in rows for cid in row['witness_candidate_ids']]
    ids = list(dict.fromkeys(pool))
    witnesses = ids if len(ids)<=24 else [ids[0], *ids[-23:]]
    return dict(covered_input_ids=[row['input_id'] for row in rows],
        summary='Synthetic transport-only summary; semantic accuracy is not evaluated.',
        witness_candidate_ids=witnesses, limitations=[],
        **{flag: any(row.get(flag, False) for row in rows) for flag in
           ('unresolved_conflict','unresolved_identity','requires_detail')})
