"""Build the messages asking a model to review claims against their source passages.

The review request includes the original text, proposed statements, referenced
entities and research goals. It does not supply the extractor's confidence or
another reviewer's verdict as a reason to accept the claims. A separate review
request can still use the same underlying model and share its mistakes; it is not
independent factual verification.

Single-passage and grouped requests preserve each claim's original source context.
These builders prepare messages for the structured model executor; they do not
send the request or decide the resulting support status.
"""
import json
from projectclarity.prompt_context import research_context
from projectclarity.source_context import source_context

from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('review_claims.txt')


def review_messages(session, attempt, candidates, text):
    """Assemble a separate review task so the model evaluates proposed claims against their
    original passage, scope and human guidance rather than trusting extraction alone.

    The caller verifies source text before this builder slices the passage. Building
    messages does not send a request or assign a claim status. Include original guidance
    questions and the investigation date. Shared source context retains acquisition
    facts and source-stated metadata; it does not establish currentness by itself.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        attempt: ExtractionAttempt identifying the passage and original extraction batch
            being reviewed.
        candidates: CandidateClaim objects from this batch offered to the reviewer.
        text: Hash-verified normalized text of the source document; passage offsets select
            the review excerpt.

    Returns:
        Ordered system and human message pairs containing one bounded candidate-review task.
    """
    payload = {"original_question": session.request.question,
        "goals": [goal.model_dump(mode="json") for goal in session.plan.goals],
        "scope_in": session.plan.scope_in, "scope_out": session.plan.scope_out,
        **research_context(session),
        "source_id": attempt.source_id,
        "source_context": source_context(session, attempt.source_id),
        "passage": {"passage_id": attempt.passage_id, "text": text[attempt.start:attempt.end]},
        "entities": [entity.model_dump(mode="json") for entity in attempt.result.entities],
        "candidates": [{"candidate_id": candidate.candidate_id,
            "proposal": candidate.proposal.model_dump(mode="json")} for candidate in candidates]}
    return [("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))]


def batch_review_messages(session, groups):
    """Pack complete source-bound review groups into one request without clipping claims.

    Args:
        session: Run supplying question, scope and human guidance.
        groups: Ordered (attempt, candidates, verified full text) tuples. Each
            candidate belongs only to its tuple's original extraction passage.

    Returns:
        System/human messages with explicit batch references. Single-passage
        batches retain the existing input shape; multi-passage groups keep their
        own entity namespaces, source IDs, source context and complete passage text.
        Metadata from the first passage is never applied to neighboring sources.
    """
    if len(groups) == 1:
        return review_messages(session, *groups[0])
    payloads = [json.loads(review_messages(session, *group)[1][1]) for group in groups]
    payload = {key: value for key, value in payloads[0].items()
        if key not in {'source_id', 'source_context', 'passage', 'entities', 'candidates'}}
    payload['passages'] = [{'batch_id': attempt.batch_id,
        **{key: data[key] for key in ('source_id', 'source_context', 'passage', 'entities')}}
        for (attempt, _, _), data in zip(groups, payloads)]
    payload['candidates'] = [{**candidate, 'batch_id': attempt.batch_id,
        'source_id': attempt.source_id, 'passage_id': attempt.passage_id}
        for (attempt, _, _), data in zip(groups, payloads) for candidate in data['candidates']]
    return [("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))]
