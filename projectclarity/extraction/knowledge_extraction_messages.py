"""Load user-owned extraction instructions and assemble candidate-knowledge inputs.

User-owned task wording lives in prompts/extract_knowledge.txt. The LangGraph
node lives in nodes/extract_knowledge/node.py; it decides WHEN to call this adapter and
enforces budgets. Here, a research plan supplies questions to investigate and a
single saved passage supplies possible evidence. Neither plan assumptions nor
instructions embedded in a web page become established facts or tool commands.

ExtractionBatch, defined in extraction_contracts.py, is the actual output schema.
LangChain sends it through the provider's structured-output API; mentioning a
field in this prompt alone does not create that field. Native citation blocks
are not enabled alongside strict JSON output. Instead, Python verifies quoted
text later. No tool, search, file-write or publication permission is given to the
model. A well-formed answer can still contain false or unsupported assertions.
"""

import json

from projectclarity.providers.structured_model_provider import create_structured_model

from projectclarity.extraction.extraction_contracts import ExtractionBatch
from projectclarity.prompt_context import research_context


from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('extract_knowledge.txt')


def create_extractor(api_key: str, session):
    """Build, but do not invoke, the bounded Anthropic structured-output adapter.

    include_raw gives the node answer text and usage even when schema parsing fails. The
    node saves selected fields privately; it never dumps the SDK object. No tool permissions
    or automatic retries are attached to this call.

    Args:
        api_key: Transient model-provider credential; used to configure the client, not
            saved as research data.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.

    Returns:
        A configured structured-output runnable; no request is sent until the caller invokes
        it.
    """
    return create_structured_model(api_key, session, ExtractionBatch)


def extraction_messages(session, attempt, text: str) -> list[tuple[str, str]]:
    """Build system/task messages tying one saved passage to the plan's research goals.

    This builder does not send a request. Source IDs identify evidence without adding local
    path metadata or credential settings. The node verifies text hashes and checks the
    configured key before invoking the model. No prior passage replies or link inventory are
    included; this is not cross-source reasoning. Human answers include their original
    questions when recoverable, plus working assumptions and the investigation date.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        attempt: ExtractionAttempt identifying the source, passage offsets and batch for
            this request.
        text: Hash-verified normalized source-document text from which this attempt's
            passage is sliced.

    Returns:
        Ordered system and human message pairs asking for structured candidates from one
        source passage.
    """
    plan = session.plan
    payload = {
        "original_question": session.request.question,
        **research_context(session),
        "objective": plan.objective,
        "goals": [goal.model_dump(mode="json") for goal in plan.goals],
        "completion_criteria": [c.model_dump(mode="json") for c in plan.completion_criteria],
        "scope_in": plan.scope_in, "scope_out": plan.scope_out,
        "source_id": attempt.source_id,
        "passage": {"passage_id": attempt.passage_id, "text": text[attempt.start:attempt.end]},
    }
    return [("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))]
