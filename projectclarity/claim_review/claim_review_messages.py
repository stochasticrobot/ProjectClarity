"""Prepare an independent review task for any compatible structured-output adapter.

The reviewer sees the original passage, candidate assertions, entity definitions,
and research goals. It does not see another reviewer's verdict or the extractor's
confidence. Independence here means a separate call/task, not a different model
or a guaranteed independent error distribution. The current adapter is Anthropic.
"""
import json
from projectclarity.prompt_context import research_context

from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('review_claims.txt')


def review_messages(session, attempt, candidates, text):
    """Assemble a separate review task so the model evaluates proposed claims against their
    original passage, scope and human guidance rather than trusting extraction alone.

    The caller verifies source text before this builder slices the passage. Building
    messages does not send a request or assign a claim status. Include original guidance
    questions and the investigation date; source hashes remain application metadata.

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
        "passage": {"passage_id": attempt.passage_id, "text": text[attempt.start:attempt.end]},
        "entities": [entity.model_dump(mode="json") for entity in attempt.result.entities],
        "candidates": [{"candidate_id": candidate.candidate_id,
            "proposal": candidate.proposal.model_dump(mode="json")} for candidate in candidates]}
    return [("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))]
