"""Capture optional source observations in scheduled quanta, committing each batch.

The scheduler calls this node for retrieved text within the auxiliary allowance. It does not receive the research scope
as a filter: a source-supported entity remains useful with unknown answer relevance.
Overlapping text windows keep nearby names/affiliations together where possible;
this is not a general DOM/table parser. Continuation and pending windows are explicit,
but model-declared completion is not independently verified extraction recall.
"""
import json
from collections import Counter

from projectclarity.extraction.saved_source_passages import read_verified_text, EvidenceInputError
from projectclarity.extraction.source_quote_matching import match_source_quote, QuoteMatchError
from projectclarity.knowledge.contracts import KnowledgeBatch
from projectclarity.knowledge.graph_store import commit_observations, stable_id
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.session_storage import save_session, write_text
from projectclarity.knowledge.capture_budget import capture_remaining, capture_requests


from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('capture_knowledge.txt')


def capture_windows(text, size=2400, overlap=300):
    """Divide saved text into overlapping windows so dense lists get bounded capture calls.

    Args:
        text: Complete hash-verified normalized source text.
        size: Maximum body characters per window, at least twice the overlap.
        overlap: Context characters repeated near the previous boundary to reduce split rows.

    Returns:
        A list of (start, end) character offsets covering the text. Newline boundaries are
        preferred; unusually long records may still split and completeness is not inferred.
    """
    if overlap < 0 or size <= max(1, 2 * overlap):
        raise ValueError("Invalid capture window bounds")
    windows, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        windows.append((start, end))
        if end == len(text):
            break
        next_start = max(start + 1, end - overlap)
        boundary = text.rfind("\n", max(start + 1, next_start - 120), next_start)
        start = boundary + 1 if boundary >= 0 else next_start
    return windows


def anchor_quote(quote, text, spans):
    """Locate one quote within the exact input spans so evidence cannot come from unseen text.

    Args:
        quote: Proposed evidence quotation from one entity or assertion.
        text: Full verified normalized source text.
        spans: Character ranges supplied as body/context in this capture call.

    Returns:
        Original text and absolute offsets for one unique location. Missing or ambiguous
        evidence raises QuoteMatchError; overlapping context does not duplicate a location.
    """
    locations = {}
    for start, end in spans:
        try:
            match = match_source_quote(text[start:end], quote)
        except QuoteMatchError as exc:
            if str(exc) == "ambiguous_quote":
                raise
            continue
        locations[(start + match.start, start + match.end)] = match.quote
    if len(locations) != 1:
        raise QuoteMatchError("ambiguous_quote" if locations else "quote_not_found")
    (start, end), original = next(iter(locations.items()))
    return {"start": start, "end": end, "quote": original}


def validate_capture(batch, text, spans):
    """Validate entity mentions and individual assertions independently to retain usable evidence.

    Args:
        batch: Parsed KnowledgeBatch proposed by the capture model.
        text: Source text reverified after the provider response.
        spans: Exact body/context ranges offered to that model call.

    Returns:
        A tuple of validated entity dictionaries, validated assertion dictionaries and
        rejected-record diagnostics. Anchoring proves location, not semantic truth.
    """
    counts = Counter(entity.entity_id for entity in batch.entities)
    entities, assertions, rejected = [], [], []
    for entity in batch.entities:
        try:
            if counts[entity.entity_id] != 1:
                raise ValueError("duplicate_entity_id")
            if " ".join(entity.name.casefold().split()) not in " ".join(entity.quote.casefold().split()):
                raise ValueError("name_not_in_quote")
            evidence = anchor_quote(entity.quote, text, spans)
        except (ValueError, QuoteMatchError) as exc:
            rejected.append({"kind": "entity", "id": entity.entity_id, "reason": str(exc)})
        else:
            entities.append({**entity.model_dump(exclude={"quote"}), "evidence": evidence})
    valid_ids = {entity["entity_id"] for entity in entities}
    for index, assertion in enumerate(batch.assertions):
        try:
            if (assertion.object_id is None) == (assertion.value is None):
                raise ValueError("choose_relationship_or_attribute")
            if assertion.subject_id not in valid_ids or (assertion.object_id and assertion.object_id not in valid_ids):
                raise ValueError("unknown_or_invalid_entity")
            if assertion.object_id is None and assertion.role is not None:
                raise ValueError("role_requires_relationship")
            evidence = anchor_quote(assertion.quote, text, spans)
        except (ValueError, QuoteMatchError) as exc:
            rejected.append({"kind": "assertion", "index": index, "reason": str(exc)})
        else:
            assertions.append({**assertion.model_dump(exclude={"quote"}), "evidence": evidence})
    return entities, assertions, rejected


def save_capture(session, output_root):
    """Persist current capture and source-yield receipts so interruptions leave honest progress.

    Args:
        session: ResearchSession with accumulated capture windows, yields and graph cursor.
        output_root: Local directory containing run artifacts and knowledge.sqlite3.

    Returns:
        None. Refresh knowledge.json, source_value.json and session.json without a model call.
    """
    windows = session.knowledge["windows"]
    session.knowledge["incomplete"] = any(window["status"] != "captured" for window in windows)
    session.knowledge['objective_status'] = 'incomplete' if session.knowledge['incomplete'] else 'complete'
    session.knowledge['requests_attempted'] = capture_requests(session)
    session.knowledge['max_requests'] = session.request.knowledge_max_requests
    directory = output_root / session.session_id
    write_text(directory / "knowledge.json", json.dumps(session.knowledge, indent=2, ensure_ascii=False) + "\n")
    write_text(directory / "source_value.json", json.dumps(session.source_value, indent=2) + "\n")
    save_session(session, output_root)


def accumulate_knowledge(state, output_root, key_reader=read_api_key, monitor=None, *, final_pass=False, max_calls=None):
    """Capture and commit graph observations before selecting claims for the current answer.

    Args:
        state: Graph state containing a typed ResearchSession and retrieved source receipts.
        output_root: Local run root; knowledge.sqlite3 here accumulates across these runs.
        key_reader: Zero-argument credential reader, called only for authorized model work.
        monitor: Optional progress/cancellation monitor shared by the graph invocation.
            After each saved batch, emit its committed cursor range for private console
            display; graph names, values and quotes do not enter operational events.
        max_calls: Optional scheduler quantum overriding per-round capture calls;
            final_pass can remove research reservation without removing this quantum.
        final_pass: Drain pending windows after discovery ends instead of reserving calls
            for discovery/extraction. Both modes obey the auxiliary capture allowance
            and the unchanged shared model request ceiling.

    Returns:
        A session state update with capture progress, rejected evidence, database location
        and change cursor. Every successful batch commits immediately. Failures/budget stops
        preserve earlier records and mark pending work; no claim becomes an accepted fact.
    """
    session = state["session"]
    session.visited_nodes.append("accumulate_knowledge")
    store = output_root / "knowledge.sqlite3"
    knowledge = session.knowledge
    knowledge.setdefault("schema_version", 1)
    knowledge.setdefault("windows", [])
    knowledge["database"] = str(store)
    knowledge["completion_basis"] = "model_reported_window_completion_not_verified_entity_recall"
    registered = {window["source_id"] for window in knowledge["windows"]}
    texts = {}
    documents = {document.source_id: document for document in session.documents}
    for document in session.documents:
        if document.status != "retrieved":
            continue
        try:
            text = read_verified_text(output_root / session.session_id, document)
        except EvidenceInputError as exc:
            knowledge["windows"].append({"source_id": document.source_id, "status": "failed", "error": str(exc), "calls": []})
            continue
        texts[document.source_id] = text
        if document.source_id in registered:
            continue
        for index, (start, end) in enumerate(capture_windows(text), 1):
            knowledge["windows"].append({"source_id": document.source_id, "window_id": f"{document.source_id}_W{index}",
                "start": start, "end": end, "text_sha256": document.text_sha256, "status": "pending", "calls": []})
    save_capture(session, output_root)
    quantum = max_calls if max_calls is not None else None if final_pass else session.request.knowledge_calls_per_round
    reserve = 0 if final_pass or quantum is None else (1 + int(session.request.review_claims)
        + int(session.request.assess_coverage) + int(session.request.follow_links or session.request.web_search))
    calls_this_round = 0
    knowledge['pause_reason'] = None
    while True:
        pending = [w for w in knowledge['windows'] if w['status'] == 'pending']
        if not pending:
            break
        if capture_remaining(session) <= 0:
            knowledge['pause_reason'] = ('request_limit' if session.usage.requests_attempted >=
                session.request.budget.max_requests else 'knowledge_request_limit')
            break
        if session.status == 'cancelled':
            knowledge['pause_reason'] = 'cancelled'
            break
        if session.usage.requests_attempted >= session.request.budget.max_requests - reserve:
            knowledge['pause_reason'] = 'request_limit' if not reserve else 'calls_reserved_for_research'
            break
        if quantum is not None and calls_this_round >= quantum:
            knowledge['pause_reason'] = 'scheduling_quantum'
            break
        # Lowest-served sources go first; within a source, unseen windows precede
        # continuations. This schedules all content without a topic-specific pool.
        window = min(pending, key=lambda w: (
            session.source_value.get(w['source_id'], {}).get('capture_model_requests', 0),
            len(w['calls']), w['start']))
        document = documents[window['source_id']]
        text = texts.get(document.source_id)
        if text is None or window['text_sha256'] != document.text_sha256:
            window.update(status='failed', error='saved_capture_text_unavailable_or_changed')
            continue
        spans = [(window["start"], window["end"])]
        if window["start"] > 0:
            spans.append((0, min(900, len(text))))
        prior = window.setdefault('batches', [])
        seen = set(window.get('fingerprints', []))
        call_id = f"knowledge_{window['window_id']}_{len(window['calls']) + 1}"
        payload = {"source_id": document.source_id, "window_id": window["window_id"],
            "source_location": document.final_url or document.original_location,
            "spans": [{"start": start, "end": end, "text": text[start:end]} for start, end in spans],
            "already_captured": prior}
        answer, receipt = invoke_structured(session, output_root, call_id=call_id,
            task="accumulate_knowledge", prompt_version="knowledge-capture-v3",
            messages=[("system", SYSTEM_PROMPT), ("human", json.dumps(payload, ensure_ascii=False))],
            schema=KnowledgeBatch, key_reader=key_reader, monitor=monitor)
        window["calls"].append(receipt["call_id"])
        calls_this_round += 1
        value = session.source_value.setdefault(document.source_id, {})
        attempted = receipt["status"] != "not_attempted"
        value["capture_model_requests"] = value.get("capture_model_requests", 0) + attempted
        if attempted:
            usage = value.setdefault("capture_tokens", {"input_tokens": 0, "output_tokens": 0})
            for field in ("input_tokens", "output_tokens"):
                reported = receipt[field]
                usage[field] = usage[field] + reported if usage[field] is not None and reported is not None else None
        if answer is None:
            window.update(status="failed", error=receipt["error_code"])
            save_capture(session, output_root)
            if receipt["error_code"] in {"cancelled", "provider_or_adapter_error", "request_limit"}:
                knowledge["pause_reason"] = receipt["error_code"]
                break
            continue
        try:
            verified = read_verified_text(output_root / session.session_id, document)
            entities, assertions, rejected = validate_capture(answer, verified, spans)
        except EvidenceInputError as exc:
            window.update(status="failed", error=str(exc))
            continue
        window.setdefault("rejected", []).extend(rejected)
        result = commit_observations(store, session.session_id,
            {"location": document.final_url or document.original_location, "text_sha256": document.text_sha256,
             "retrieved_at": document.acquired_at}, entities, assertions)
        knowledge["cursor"] = result["cursor"]
        observed = value.setdefault("observed_yield", {})
        for counter in ("entity_mentions", "assertions", "rejected_records"):
            observed.setdefault(counter, 0)
        observed["entity_mentions"] += len(entities)
        observed["assertions"] += len(assertions)
        observed["rejected_records"] += len(rejected)
        # Yield counts include overlapping-window mentions; database inserts deduplicate.
        value["yield_basis"] = "validated_capture_records_including_overlap_not_unique_people"
        fingerprint = stable_id("reply", entities, assertions)
        if not answer.remaining:
            window["status"] = "partial" if window.get("rejected") else "captured"
        elif fingerprint in seen or (not entities and not assertions):
            window.update(status="partial", error="continuation_made_no_progress")
        seen.add(fingerprint)
        window["fingerprints"] = sorted(seen)
        prior.append({"entities": [e.model_dump() for e in answer.entities],
                      "assertions": [a.model_dump() for a in answer.assertions]})
        save_capture(session, output_root)
        if monitor:
            monitor.observe("knowledge_committed", stage="accumulate_knowledge", source_id=document.source_id,
                candidate_count=len(assertions), response_entities=len(entities),
                knowledge_cursor_start=result["cursor_start"], knowledge_cursor_end=result["cursor"])
    save_capture(session, output_root)
    return {"session": session}
