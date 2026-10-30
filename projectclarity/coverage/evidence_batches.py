"""Split checked evidence into size-limited inputs for completion assessment.

For a large research run, the coverage reviewer cannot receive every source claim
in one request. These helpers partition the supplied evidence, attach its review
outcomes and source metadata, and assign hashes identifying each exact input.
The hashes let assessment code reuse an earlier batch result only when the input
and saved result are still valid.

The caller must first check source integrity and later validate model replies.
This module only prepares batches and checks reuse records; it makes no model
requests and cannot decide whether the answer is complete.
"""
import hashlib
import json

from projectclarity.discovery.inventory_context import payload_messages
from projectclarity.source_context import source_context


def fingerprint(value):
    """Identify the exact JSON input to a reusable evidence assessment.

    Args:
        value: JSON-compatible contract, evidence, source metadata or receipt data.
    Returns:
        Full SHA-256 digest of canonical UTF-8 JSON; no state or files are changed.
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def batch_payload(session, candidates, context):
    """Build the complete model input for one group of source-checked claims.

    Include each claim's quotations, review outcome and source metadata together with
    the fixed answer requirements. This keeps a later summary attributable to the
    evidence actually offered rather than a clipped statement alone.

    Args:
        session: Run owning the candidate reviews and acquired source receipts.
        candidates: Ordered, source-checked CandidateClaim objects for one batch.
        context: JSON-compatible answer contract and scope/guidance snapshot.
    Returns:
        A new payload containing every candidate and complete evidence anchors,
        plus only the source records relevant to this batch. No text is clipped,
        no model verdict is inferred and the session is unchanged.
    """
    return dict(context=context, candidates=[c.model_dump(mode='json') for c in candidates],
        reviews={c.candidate_id: session.claim_reviews.get(c.candidate_id) for c in candidates},
        sources={sid: source_context(session, sid) for sid in
            dict.fromkeys(c.source_id for c in candidates)})


def plan_evidence_batches(session, valid, context, system_prompt, *, max_characters,
                          max_candidates=64):
    """Divide all supplied checked claims into requests that fit the configured size limit.

    Every claim must appear exactly once. A batch gets a hash of its complete input
    so an unchanged earlier summary can be reused. If even one complete claim cannot
    fit, report the problem instead of truncating evidence or silently discarding it.

    Args:
        session: Run owning source context and saved claim reviews.
        valid: Ordered mapping of freshly source-checked candidate IDs to claims.
        context: Complete contract/guidance snapshot, excluding changing progress.
        system_prompt: Exact caller-supplied approved prompt; included in cache keys.
        max_characters: Positive serialized-message allowance including prompt text.
        max_candidates: Maximum candidates per batch to bound output obligations;
            defaults to 64, never a cap on the total eligible evidence population.
    Returns:
        Ordered batch descriptors with payload, candidate IDs, size and input digest.
        Every supplied candidate appears exactly once. Completed full batches stay
        stable when evidence is appended; changed inputs invalidate affected keys.
        Raises ValueError if a single complete item or base context cannot fit,
        rather than discarding or truncating it. No model calls or state mutation.
    """
    if max_characters <= 0 or max_candidates <= 0:
        raise ValueError('invalid_evidence_batch_limit')
    batches, pending = [], []

    def descriptor(rows):
        """Measure the actual serialized input and give it a content-based key.

        Args:
            rows: Ordered candidates considered for a single complete batch.
        Returns:
            Descriptor including the full payload and exact input revision;
            reads enclosing context only, with no persistent side effects.
        """
        payload = batch_payload(session, rows, context)
        messages = payload_messages(system_prompt, payload)
        return dict(batch_id='EB_' + fingerprint(messages),
            candidate_ids=[c.candidate_id for c in rows], payload=payload,
            characters=len(json.dumps(messages, ensure_ascii=False)))

    if descriptor([])['characters'] > max_characters:
        raise ValueError('evidence_batch_base_too_large')
    for candidate_id, candidate in valid.items():
        if candidate_id != candidate.candidate_id:
            raise ValueError('evidence_batch_candidate_id_mismatch')
        trial = descriptor([*pending, candidate])
        if pending and (len(pending) >= max_candidates or trial['characters'] > max_characters):
            batches.append(descriptor(pending))
            pending = []
            trial = descriptor([candidate])
        if trial['characters'] > max_characters:
            raise ValueError('evidence_batch_item_too_large:' + candidate_id)
        pending.append(candidate)
    if pending:
        batches.append(descriptor(pending))
    return batches


def reusable_batch(batch, receipt):
    """Check whether a saved batch summary is valid for this exact assessment input.

    Reuse requires a completed error-free record, matching input identity, intact
    output and complete accounting for the offered claims. Passing this check does
    not establish the summary's factual accuracy.

    Args:
        batch: Current descriptor from plan_evidence_batches after source checks.
        receipt: Optional previously validated assessment and its retained input ID.
    Returns:
        True only for a completed, error-free receipt of these exact inputs with
        complete candidate accounting. This validates reuse, not summary accuracy,
        overall coverage, evidence truth or permission to stop.
    """
    return bool(receipt and receipt.get('status') == 'completed'
        and not receipt.get('error_code') and receipt.get('batch_id') == batch['batch_id']
        and set(receipt.get('covered_candidate_ids', [])) == set(batch['candidate_ids'])
        and receipt.get('assessment') is not None)
