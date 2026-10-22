"""Implement the extract_knowledge LangGraph node: read evidence with an LLM.

START HERE for the extraction feature. After retrieval, this node lists the
saved passages, asks the model about one passage at a time, and records every
outcome. For example, S1/P1 may produce two claims while S1/P2 has no findings;
both are completed work. A passage skipped because the request budget ran out
is NOT a completed empty result. You can inspect those differences on disk.

The graph registers extract_knowledge() from THIS file. It receives the run's
session and returns the updated session, just like the existing planning node.
It does not print a UI, search, approve claims, or choose publication policy.
The next nodes separately check evidence and save inspectable candidates.

Where to change things:
  knowledge_extraction_messages.py: the prompt and LangChain/Anthropic adapter.
  extraction_contracts.py: the model's reply and local receipt formats.
  saved_source_passages.py: bounded, hash-checked reading and passage boundaries.
  validate_evidence_node.py: reference and quotation checks, without an LLM.
  candidate_claim_storage.py: JSON/Markdown outputs, never accepted knowledge.
  extraction_call_tracing.py: private prompts, answer text and per-call diagnostics.
  extraction_inspection.py: human-readable summaries and saved-call inspection.

Controls: extraction is opt-in; its calls share --max-requests with planning.
Each call reserves its attempt BEFORE invocation, has the configured output cap
and a 60-second provider timeout, and is never automatically retried. Cancellation
is checked between calls; a blocking call must return before cooperative cancel
can take effect. Receipts survive partial failures, but automatic crash/resume
of extraction is not implemented. Source text goes to the configured provider
only when this feature is enabled; no API key enters graph state or artifacts.
"""

import hashlib
import json
import time
from pathlib import Path

from projectclarity.extraction.candidate_claim_storage import save_extraction_record
from projectclarity.extraction.extraction_call_tracing import start_trace, capture_answer, finish_trace
from projectclarity.extraction.extraction_contracts import ExtractionAttempt, ExtractionBatch, ExtractionRecord
from projectclarity.extraction.knowledge_extraction_messages import SYSTEM_PROMPT, create_extractor, extraction_messages
from projectclarity.extraction.saved_source_passages import EvidenceInputError, iter_passages, read_verified_text
from projectclarity.research_planner import read_api_key
from projectclarity.run_monitor import MonitoringError
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import write_text


def extract_knowledge(state: dict, output_root: Path, key_reader=read_api_key, monitor=None) -> dict:
    """Inventory saved passages, make sequential budgeted calls and preserve each outcome.

    The plan tells the model what to seek; it is not source evidence. One call
    sees one passage, not the whole corpus or previous replies. Empty lists are
    valid completed work; unattempted passages remain visible. Malformed output
    fails that passage, while provider/adapter errors stop later calls. Validation
    and candidate storage follow in separate nodes, including after partial work.
    Follow-up invocations append only newly retrieved sources. Prior attempted,
    failed and budget-skipped passages remain receipts, not automatic retries.
    """
    session = state["session"]
    session.visited_nodes.append("extract_knowledge")
    directory = output_root / session.session_id
    record = ExtractionRecord.model_validate(session.extraction) if session.extraction else ExtractionRecord()
    record.retrieval_incomplete = record.retrieval_incomplete or session.status == "retrieval_partial"
    previous_batches = {attempt.batch_id for attempt in record.attempts}
    previous_sources = {attempt.source_id for attempt in record.attempts} | {error["source_id"] for error in record.source_errors}
    session.status = "extracting"
    save_extraction_record(session, record, output_root)
    # Save the exact non-secret instructions/settings for reproducibility. Each
    # attempt additionally fingerprints its full messages, including source text.
    write_text(directory / "extraction_config.json", json.dumps({
        "schema_version": "1.0", "prompt_version": record.prompt_version,
        "system_prompt": SYSTEM_PROMPT, "model": session.request.model,
        "max_output_tokens": session.request.budget.max_output_tokens,
        "max_requests_including_planning": session.request.budget.max_requests,
        "effort": "low", "timeout_seconds": 60, "max_retries": 0,
        "max_serialized_message_characters": 48_000,
    }, indent=2) + "\n")
    current = None
    try:
        if monitor:
            monitor.observe("extraction_started", stage="extracting", run_status="extracting",
                llm_requests=session.usage.requests_attempted)
            monitor.check_cancel()
        documents = {doc.source_id: doc for doc in session.documents if doc.status == "retrieved"}
        # Inventory every readable passage first, so a budget stop cannot hide
        # the fact that some evidence was never examined. Only one text is held
        # at a time; unreadable versions receive explicit source-level errors.
        for document in documents.values():
            if document.source_id in previous_sources:
                continue
            if monitor:
                monitor.check_cancel()
            try:
                text = read_verified_text(directory, document)
                if not text.strip():
                    raise EvidenceInputError("empty_saved_text")
                for passage_id, start, end in iter_passages(text):
                    record.attempts.append(ExtractionAttempt(
                        batch_id=f"{document.source_id}_{passage_id}", source_id=document.source_id,
                        text_sha256=document.text_sha256, passage_id=passage_id, start=start, end=end))
            except EvidenceInputError as exc:
                record.source_errors.append({"source_id": document.source_id, "error_code": str(exc)})
        save_extraction_record(session, record, output_root)
        extractor, api_key = None, None
        for current in record.attempts:
            if current.batch_id in previous_batches:
                continue
            if monitor:
                monitor.check_cancel()
            if session.usage.requests_attempted >= session.request.budget.max_requests:
                current.error_code = "request_limit"
                continue
            started = time.monotonic()
            trace, stop_after_error = None, False
            try:
                # Re-read before each call: a changed file must not inherit a
                # hash from the earlier inventory. Never trust model-supplied paths.
                text = read_verified_text(directory, documents[current.source_id])
                messages = extraction_messages(session, current, text)
                serialized = json.dumps(messages, ensure_ascii=False)
                if len(serialized) > 48_000:
                    raise EvidenceInputError("extraction_input_too_large")
                current.input_sha256 = hashlib.sha256(serialized.encode()).hexdigest()
                if extractor is None:
                    api_key = key_reader()
                    extractor = create_extractor(api_key, session)
                # This private trace is separate from operational logging. Save
                # the actual application messages before invocation, and never
                # let a configured credential become document/prompt content.
                trace = start_trace(directory, session, current, messages, api_key)
                current.trace_path = f"extraction_calls/{current.batch_id}.json"
                if api_key and api_key in serialized:
                    raise EvidenceInputError("credential_in_input")
                if monitor:
                    monitor.check_cancel()
                current.status = "started"
                session.usage.requests_attempted += 1
                save_extraction_record(session, record, output_root)
                if monitor:
                    monitor.observe("extraction_call_started", source_id=current.source_id,
                        stage="extracting", llm_requests=session.usage.requests_attempted,
                        passages_total=len(record.attempts), batch_id=current.batch_id,
                        passage_id=current.passage_id, trace_path=current.trace_path)
                if monitor:
                    monitor.model_input("extract_knowledge", current.batch_id, messages, ExtractionBatch, api_key)
                response = extractor.invoke(messages)
                raw = response["raw"]
                if monitor:
                    monitor.model_output("extract_knowledge", current.batch_id, raw, api_key)
                # Preserve answer text before checking the parser result: a
                # malformed reply is useful for private debugging, not a claim.
                capture_answer(trace, raw)
                usage = raw.usage_metadata or {}
                for field in ("input_tokens", "output_tokens", "total_tokens"):
                    value = usage.get(field)
                    setattr(current, field, value)
                    prior = getattr(session.usage, field)
                    # Unknown usage remains unknown, never a misleading zero.
                    setattr(session.usage, field, prior + value if prior is not None and value is not None else None)
                current.response_model = raw.response_metadata.get("model_name")
                current.stop_reason = raw.response_metadata.get("stop_reason")
                session.usage.response_model = current.response_model
                session.usage.stop_reason = current.stop_reason
                if current.stop_reason != "end_turn":
                    current.status, current.error_code = "failed", "incomplete_model_output"
                elif response.get("parsing_error") is not None or response.get("parsed") is None:
                    current.status, current.error_code = "failed", "invalid_extraction_schema"
                else:
                    parsed = ExtractionBatch.model_validate(response["parsed"])
                    if api_key and api_key in parsed.model_dump_json():
                        raise EvidenceInputError("credential_in_response")
                    current.result = parsed
                    current.status = "completed"
            except EvidenceInputError as exc:
                current.status, current.error_code = "failed", str(exc)
            except (RunCancelled, KeyboardInterrupt, EOFError, MonitoringError):
                if current.status == "started":
                    current.status, current.error_code = "failed", "interrupted"
                    session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
                raise
            except Exception:
                if current.status == "started":
                    session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
                current.status, current.error_code = "failed", "provider_or_adapter_error"
                # Authentication/transport failures should not cause a storm of
                # requests to other passages. Preserve them as unattempted work.
                for pending in record.attempts:
                    if pending.status == "not_attempted":
                        pending.error_code = "stopped_after_provider_error"
                stop_after_error = True
            finally:
                # Run for failure/cancellation too, so the last visible event
                # is not just "started". These files are separate atomic writes,
                # not a transaction; a process crash can leave partial diagnostics.
                current.elapsed_seconds = round(time.monotonic() - started, 3)
                if trace is not None:
                    finish_trace(directory, trace, current, api_key)
                save_extraction_record(session, record, output_root)
                if monitor:
                    monitor.observe("extraction_call_finished", source_id=current.source_id,
                        stage="extracting", passages_completed=sum(a.status == "completed" for a in record.attempts),
                        passages_total=len(record.attempts), error_type=current.error_code,
                        batch_id=current.batch_id, call_status=current.status,
                        response_entities=len(current.result.entities) if current.result else 0,
                        response_claims=len(current.result.claims) if current.result else 0,
                        input_tokens=current.input_tokens, output_tokens=current.output_tokens,
                        call_seconds=current.elapsed_seconds, stop_reason=current.stop_reason,
                        trace_path=current.trace_path)
            if stop_after_error:
                break
        if monitor:
            monitor.check_cancel()
    except (RunCancelled, KeyboardInterrupt, EOFError):
        session.status = "cancelled"
        session.error = "Extraction cancelled; completed proposals were retained. Unfinished request usage may be unknown."
        if current is not None and current.status == "started":
            current.status, current.error_code = "failed", "cancelled"
            session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
        for pending in record.attempts:
            if pending.status == "not_attempted":
                pending.error_code = "cancelled"
    save_extraction_record(session, record, output_root)
    if monitor:
        monitor.observe("extraction_finished", stage=session.status, run_status=session.status,
            passages_total=len(record.attempts),
            passages_completed=sum(a.status == "completed" for a in record.attempts))
    return {"session": session}
