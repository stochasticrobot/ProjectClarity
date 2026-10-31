"""Prepare the messages and model adapter used to extract claims from one source passage.

Task instructions are loaded verbatim from the extraction prompt text file. The
message builder adds the passage, its source context and the research goals so the
model can propose claims and result fields with quotations. The response format
comes from the extraction schema, not from an example answer embedded here.
Creating the adapter does not invoke it; the extraction node sends the request,
accounts for usage and saves the outcome.
"""

import json

from projectclarity.providers.structured_model_provider import create_structured_model

from projectclarity.extraction.extraction_contracts import ExtractionBatch
from projectclarity.extraction.record_refinement import refinement_context
from projectclarity.prompt_context import record_task_context
from projectclarity.source_context import source_context


from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('extract_knowledge.txt')


def create_extractor(api_key: str, session, *, timeout_seconds=None):
    """Build, but do not invoke, the bounded Anthropic structured-output adapter.

    include_raw gives the node answer text and usage even when schema parsing fails. The
    node saves selected fields privately; it never dumps the SDK object. No tool permissions
    or SDK retries are attached to this call; the shared executor owns bounded recovery.

    Args:
        api_key: Transient model-provider credential; used to configure the client, not
            saved as research data.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        timeout_seconds: Optional attempt-specific timeout; None uses the saved policy.

    Returns:
        A configured structured-output runnable; no request is sent until the caller invokes
        it.
    """
    return create_structured_model(api_key, session, ExtractionBatch, timeout_seconds=timeout_seconds)


def extraction_messages(session, attempt, text: str) -> list[tuple[str, str]]:
    """Build system/task messages tying one saved passage to the plan's research goals.

    This builder does not send a request. Source IDs identify evidence without adding local
    path metadata or credential settings. The node verifies text hashes and checks the
    configured key before invoking the model. A targeted follow-up includes bounded,
    source-matched structural defect receipts and, when explicitly assigned, the
    original proposed claims with reviewer mapping judgments. Those proposals are
    task data to check against the same passage, not new source facts. No other
    passage replies or link inventory are included; this is not cross-source reasoning.
    Human answers include their original
    questions when recoverable, plus working assumptions and the investigation date.
    Source context carries acquisition time and source-stated metadata separately;
    neither observation time nor metadata certifies currentness.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        attempt: ExtractionAttempt identifying the source, passage offsets and batch for
            this request.
        text: Hash-verified normalized source-document text from which this attempt's
            passage is sliced.

    Returns:
        Ordered system and human message pairs asking for structured candidates from one
        source passage. Full mapping obligations are not silently truncated; an
        Global study conclusions and completion criteria are omitted; scope and
        record definitions remain. An oversized message fails through the normal bounded-call policy, leaving
        the mapping gaps unresolved.
    """
    plan = session.plan
    payload = {
        "original_question": session.request.question,
        **record_task_context(session),
        "objective": plan.objective,
        "goals": [goal.model_dump(mode="json") for goal in plan.goals],
        "scope_in": plan.scope_in, "scope_out": plan.scope_out,
        "source_id": attempt.source_id,
        "source_context": source_context(session, attempt.source_id),
        "passage": {"passage_id": attempt.passage_id, "text": text[attempt.start:attempt.end]},
    }
    if attempt.refinement_parent:
        payload['refinement'] = refinement_context(attempt, session.extraction or {}, payload['goals'], text)
        from projectclarity.claim_review.report_mapping import mapping_assessments
        data = session.model_dump(mode='json')
        ownership = session.workflow.get('report_mapping_repairs', {})
        payload['refinement']['mapping_obligations'] = [
            dict(candidate_id=c['candidate_id'], proposed_claim=c['proposal'],
                 mapping_assessments=mapping_assessments(data, c))
            for c in (session.extraction or {}).get('candidates', [])
            if ownership.get(c['candidate_id'], {}).get('batch_id') == attempt.batch_id]
    return [("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))]
