"""Implement the extract_knowledge LangGraph node: read evidence with an LLM.

START HERE for the extraction feature. After retrieval, this node lists the
saved passages, asks the model independently about each passage, and records every
outcome. For example, S1/P1 may produce two claims while S1/P2 has no findings;
both are completed work. A passage skipped because the request budget ran out
is NOT a completed empty result. You can inspect those differences on disk.

The graph registers extract_knowledge() from THIS file. It receives the run's
session and returns the updated session, just like the existing planning node.
It does not print a UI, search, approve claims, or choose publication policy.
The next nodes separately check evidence and save inspectable candidates.

Where to change things:
  knowledge_extraction_messages.py: load prompt text and build messages/model adapter.
  extraction_contracts.py: the model's reply and local receipt formats.
  saved_source_passages.py: bounded, hash-checked reading and passage boundaries.
  nodes/validate_evidence/node.py: reference and quotation checks, without an LLM.
  nodes/persist_candidates/node.py: JSON/Markdown outputs, never accepted knowledge.
  extraction_call_tracing.py: private prompts, answer text and per-call diagnostics.
  extraction_inspection.py: human-readable summaries and saved-call inspection.

Controls: extraction is opt-in; its calls share --max-requests with planning.
Each call reserves its attempt BEFORE invocation, has the configured output cap
and a configurable provider timeout. The shared executor retries only classified
transient failures within the saved retry and request allowances. Cancellation
is checked by the coordinator while calls are in flight; sent requests cannot
be recalled and may still incur usage. The central scheduler resumes unattempted passages from saved receipts after
restart; failed or uncertain in-flight model calls are not silently replayed. Source text goes to the configured provider
only when this feature is enabled; no API key enters graph state or artifacts.
"""

import hashlib
import json
import time
from pathlib import Path

from projectclarity.nodes.persist_candidates.node import save_extraction_record
from projectclarity.extraction.extraction_call_tracing import start_trace, capture_answer, finish_trace
from projectclarity.extraction.extraction_contracts import ExtractionAttempt, ExtractionBatch, ExtractionRecord
from projectclarity.extraction.knowledge_extraction_messages import SYSTEM_PROMPT, create_extractor, extraction_messages
from projectclarity.extraction.saved_source_passages import EvidenceInputError, iter_passages, read_verified_text
from projectclarity.extraction.local_identifier_recovery import recover_local_identifiers
from projectclarity.extraction.record_refinement import record_followups
from projectclarity.extraction.document_structure import section_probes, passage_order
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.run_monitor import MonitoringError
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import write_text
from projectclarity.providers.model_retry import model_attempt_steps, remaining_model_time
from projectclarity.coverage.checkpoints import model_request_ceiling
from projectclarity.providers.concurrent_calls import run_model_jobs
from projectclarity.providers.model_failures import ModelCallFailure, describe_model_failure


def extract_knowledge(state: dict, output_root: Path, key_reader=read_api_key, monitor=None, *, max_batches=None) -> dict:
    """Prepare source passages and ask the model for claims from the scheduled passages.

    Read and hash-check saved documents, recording every passage so a budget stop
    leaves unread work visible. Each requested passage can produce claims and field
    values, an empty completed result, or a recorded failure. Several independent
    requests may overlap up to the configured concurrency, but the calling thread
    owns parsing, budget accounting and saved-state updates.

    The scheduler limits how many passages this invocation may attempt. In runs using
    the answer-cycle controller, source preparation only inventories text; paid reads
    must belong to the explicit reserved passage list. Exact duplicate saved text can
    share extraction work, but does not supply independent corroborating evidence.
    Completed, failed or uncertain earlier requests are not automatically repeated.

    Missing relationship or result-field structure can queue a one-level focused
    reread; original passages are preferred before these repairs. All requests and
    retries share the run allowance and preserve any reserved final coverage request.
    Quotation validation and support review happen in later workflow steps. This
    function records proposals and execution outcomes, not an approved final answer.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        key_reader: Zero-argument callable returning the model API key when a request needs
            it; defaults to read_api_key.
        max_batches: Optional per-invocation limit; stop after this many passage attempts
            and retain all remaining passages for a later scheduler turn. Up to
            request.model_concurrency independent calls overlap, but all parsing,
            accounting and file writes remain on the coordinating thread.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    session.visited_nodes.append("extract_knowledge")
    directory = output_root / session.session_id
    record = ExtractionRecord.model_validate(session.extraction) if session.extraction else ExtractionRecord()
    # New requests use the current contract even when earlier receipts were loaded.
    record.prompt_version = "knowledge-extraction-v5"
    record.retrieval_incomplete = any(d.status != "retrieved" for d in session.documents) or len(session.documents) < len(session.sources)
    previous_batches = {attempt.batch_id for attempt in record.attempts if attempt.status != "not_attempted"}
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
        "effort": "low", "timeout_seconds": session.request.model_timeout_seconds,
        "max_retries": 0, "application_retries": session.request.model_retries,
        "max_serialized_message_characters": 48_000,
    }, indent=2) + "\n")
    from projectclarity.nodes.schedule_work.answer_controller import enabled as answer_controlled
    from projectclarity.extraction.content_identity import content_aliases
    controlled = answer_controlled(session)
    active = session.workflow.get('tasks', {}).get(session.workflow.get('active'), {})
    inventory_only = controlled and active.get('action') == 'extract_knowledge' and not active.get('batch_id')
    if controlled:
        valid_aliases = content_aliases(session)
        record.source_aliases = {sid: row for sid, row in record.source_aliases.items() if sid in valid_aliases}
        previous_sources.update(valid_aliases)
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
        canonical_versions = {}
        if controlled:
            for attempt in record.attempts:
                doc = documents.get(attempt.source_id)
                if doc and doc.text_sha256 == attempt.text_sha256:
                    canonical_versions.setdefault(doc.text_sha256, doc.source_id)
        for document in documents.values():
            if document.source_id in previous_sources:
                continue
            if monitor:
                monitor.check_cancel()
            try:
                text = read_verified_text(directory, document)
                if not text.strip():
                    raise EvidenceInputError("empty_saved_text")
                canonical = canonical_versions.get(document.text_sha256) if controlled else None
                if canonical and canonical != document.source_id:
                    # Verify bytes, not merely matching stored labels. Results stay
                    # attached to the original source; aliases are not corroboration.
                    canonical_text = read_verified_text(directory, documents[canonical])
                    if canonical_text == text:
                        record.source_aliases[document.source_id] = {
                            'source_id': canonical, 'text_sha256': document.text_sha256}
                        continue
                canonical_versions.setdefault(document.text_sha256, document.source_id)
                for passage_id, start, end in iter_passages(text):
                    record.attempts.append(ExtractionAttempt(
                        batch_id=f"{document.source_id}_{passage_id}", source_id=document.source_id,
                        text_sha256=document.text_sha256, passage_id=passage_id, start=start, end=end))
                session.workflow.setdefault('document_structure', {})[document.source_id] = {
                    'text_sha256': document.text_sha256,
                    'probes': section_probes(text, [a.model_dump() for a in record.attempts
                        if a.source_id == document.source_id])}
            except EvidenceInputError as exc:
                record.source_errors.append({"source_id": document.source_id, "error_code": str(exc)})
        # Reconcile deterministic follow-ups after a restart without replaying the
        # parent's call or any uncertain/failed child. Old plain-text runs have no
        # parser-labelled records and keep their existing passage inventory.
        known_batches = {attempt.batch_id for attempt in record.attempts}
        for attempt in list(record.attempts):
            if attempt.status != 'completed' or attempt.source_id not in documents:
                continue
            if attempt.text_sha256 != documents[attempt.source_id].text_sha256:
                continue
            try:
                text = read_verified_text(directory, documents[attempt.source_id])
                children = record_followups(attempt, text, [g.model_dump() for g in session.plan.goals])
            except EvidenceInputError:
                continue
            for child in children:
                if child.batch_id not in known_batches:
                    record.attempts.append(child)
                    known_batches.add(child.batch_id)
        save_extraction_record(session, record, output_root)
        api_key = None
        by_id = {a.batch_id: a for a in record.attempts}
        ordered = [by_id[a['batch_id']] for a in passage_order(session, [a.model_dump() for a in record.attempts])]
        ordered.sort(key=lambda a: a.batch_id != active.get('batch_id'))
        selected = active.get('batch_ids') or ([active['batch_id']]
            if controlled and active.get('batch_id') else None)
        if inventory_only:
            ordered = []
        elif selected:
            ordered = [by_id[bid] for bid in selected if bid in by_id]
        elif active.get('source_id'):
            # An inventory/first-read handoff belongs to this source. Inventory
            # all documents, but never spend its reserved calls on an older focus.
            ordered = [a for a in ordered if a.source_id == active['source_id']]
            if active.get('first_read'):
                max_batches = 1

        def extract_passage(current):
            """Prepare, execute and retain one passage without worker-owned state.

            Args:
                current: Reserved passage receipt in the shared extraction record.
            Returns:
                A generator yielding network/retry operations to the coordinator.
                Its boolean return stops further dispatch on a provider failure;
                completed peers still retain their proposals and original evidence.
            """
            nonlocal api_key
            current.error_code = None
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
                if api_key is None:
                    api_key = key_reader()
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
                save_extraction_record(session, record, output_root)
                if monitor:
                    monitor.observe("extraction_call_started", source_id=current.source_id,
                        stage="extracting", llm_requests=session.usage.requests_attempted + 1,
                        passages_total=len(record.attempts), batch_id=current.batch_id,
                        passage_id=current.passage_id, trace_path=current.trace_path)
                if monitor:
                    monitor.model_input("extract_knowledge", current.batch_id, messages, ExtractionBatch, api_key)
                response = yield from model_attempt_steps(session, output_root, task='extract_knowledge',
                    call_id=current.batch_id, messages=messages,
                    model_factory=lambda timeout: create_extractor(api_key, session, timeout_seconds=timeout),
                    monitor=monitor, secret=api_key)
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
                    # The provider may emit overlong model-local labels despite the
                    # response schema. Recover only those labels, never facts or quotes.
                    try:
                        if trace.get('response_text_truncated'):
                            raise ValueError('truncated_response')
                        parsed, aliases = recover_local_identifiers(trace['response_text'])
                        if api_key and api_key in parsed.model_dump_json():
                            raise EvidenceInputError('credential_in_response')
                        current.result = parsed
                        current.identifier_aliases = aliases
                        current.status = 'completed'
                    except EvidenceInputError:
                        raise
                    except (ValueError, KeyError, TypeError):
                        current.status, current.error_code = "failed", "invalid_extraction_schema"
                else:
                    parsed = ExtractionBatch.model_validate(response["parsed"])
                    if api_key and api_key in parsed.model_dump_json():
                        raise EvidenceInputError("credential_in_response")
                    current.result = parsed
                    current.status = "completed"
                if current.status == 'completed':
                    for child in record_followups(current, text, [g.model_dump() for g in session.plan.goals]):
                        if child.batch_id not in known_batches:
                            record.attempts.append(child)
                            if not selected:
                                ordered.append(child)
                            known_batches.add(child.batch_id)
            except EvidenceInputError as exc:
                current.status, current.error_code = "failed", str(exc)
            except (RunCancelled, KeyboardInterrupt, EOFError, MonitoringError):
                if current.status == "started":
                    current.status, current.error_code = "failed", "interrupted"
                    session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
                raise
            except Exception as exc:
                if current.status == "started":
                    session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
                current.status = 'failed'
                current.error_code = exc.code if isinstance(exc, ModelCallFailure) else 'provider_or_adapter_error'
                current.diagnostics = exc.diagnostics if isinstance(exc, ModelCallFailure) else describe_model_failure(exc, api_key)
                current.last_attempt_id = exc.attempt_id if isinstance(exc, ModelCallFailure) else None
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
            return stop_after_error

        def jobs():
            """Offer a bounded wave without charging skipped or previously attempted work.

            Args:
                None. Read the current ordered inventory and shared request limits.
            Returns:
                Passage generators, at most max_batches when configured. New
                refinement children are eligible only after their parent completes.
            """
            consumed = 0
            for attempt in ordered:
                if attempt.batch_id in previous_batches or attempt.status != 'not_attempted':
                    continue
                if max_batches is not None and consumed >= max_batches:
                    break
                if monitor:
                    monitor.check_cancel()
                remaining = remaining_model_time(session)
                if session.usage.requests_attempted >= model_request_ceiling(session, 'extract_knowledge'):
                    attempt.error_code = 'request_limit'
                    continue
                if remaining is not None and remaining <= 0:
                    attempt.error_code = 'run_time_limit'
                    continue
                consumed += 1
                yield extract_passage(attempt)

        run_model_jobs(jobs(), concurrency=session.request.model_concurrency, monitor=monitor)
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
