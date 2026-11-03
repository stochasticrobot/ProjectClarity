"""Go through a pool of claims and select those that might be relvent to
answering the original research quesiton.

Choose facts that might help answer the research question, without rewriting them.

Knowledge extraction collects information from a document even when some of it is
unrelated to the user's question. This file prepares a request asking the AI which
of those facts could contribute to the answer, using the question and its required
answer fields. A selected fact can be useful without answering the whole question.

The model returns identifiers and reasons. This code checks those identifiers and
copies the selected original facts and their evidence into a separate collection.
The full extracted information remains untouched. evidence_selection_stage.py uses
this for each saved piece of a document, and the command-line tool can run it on
one extraction result. Relevance is not a judgment that a claim has been proved.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

from projectclarity.knowledge_extraction.knowledge_extraction import Extraction, save, validate_response
from projectclarity.question_definition.requirements_definition import validate_plan

ROOT = Path(__file__).resolve().parents[1]


class SelectedFact(BaseModel):
    """A reference to an existing source fact with its relevance explanation."""
    model_config = ConfigDict(extra="forbid", strict=True)
    fact_id: str
    reason: str


class Selection(BaseModel):
    """An empty or populated selection; no model-authored replacement facts."""
    model_config = ConfigDict(extra="forbid", strict=True)
    selected_facts: list[SelectedFact]


def prepare_knowledge(data, artifact_sha256):
    """Accept a plain extraction or accepted receipt projection and assign stable fact IDs.

    Args:
        data: Decoded Step 2 entities/facts object or successful or partially accepted chunk receipt.
        artifact_sha256: SHA-256 of the original input file bytes, used to namespace
            IDs so distinct stored artifacts cannot share chunk-local fact IDs.

    Returns:
        Validated entities and facts wrapped with IDs and zero-based source indices.
        Raises ValueError for failed receipts, invalid records or dangling entity
        references. Receipt source text, when present, is used to recheck quotes;
        plain objects cannot establish quote/source agreement. No facts are filtered,
        rewritten or deduplicated. No I/O or model call occurs.
    """
    if not isinstance(data, dict):
        raise ValueError("Knowledge must be a JSON object")
    if "status" in data and data["status"] not in ("checks_pass", "checks_partial"):
        raise ValueError("Use a successful extraction receipt, not a failed or incomplete one")
    if len(artifact_sha256) != 64 or any(c not in "0123456789abcdef" for c in artifact_sha256):
        raise ValueError("Expected a SHA-256 input artifact digest")
    payload = dict(data.get("parsed", data))
    payload.pop("credential_redaction_applied", None)
    extraction = Extraction.model_validate(payload)
    ids = [entity.id for entity in extraction.entities]
    if len(ids) != len(set(ids)) or any(not value.strip() for value in ids):
        raise ValueError("Entity IDs must be unique and nonblank")
    if any(not e.name.strip() or not e.type.strip() for e in extraction.entities):
        raise ValueError("Entity names and types must not be blank")
    for fact in extraction.facts:
        if fact.subject_id not in ids or (fact.object_id is not None and fact.object_id not in ids):
            raise ValueError("Fact references an unknown entity")
        if any(not value.strip() for value in (fact.predicate, fact.value, fact.statement, fact.quote)):
            raise ValueError("Fact text fields must not be blank")
    source_checked = False
    if "request" in data:
        message = json.loads(data["request"]["messages"][0]["content"])
        chunk = message["document_chunk"]
        if not isinstance(chunk, str):
            raise ValueError("Receipt document chunk must be text")
        validate_response(json.dumps(payload), chunk)
        source_checked = True
    return {"entities": [e.model_dump() for e in extraction.entities],
            "facts": [{"fact_id": f"{artifact_sha256}:F{index + 1}", "source_fact_index": index,
                       "fact": fact.model_dump()} for index, fact in enumerate(extraction.facts)],
            "literal_quotes_rechecked": source_checked}


def selection_request(question, requirements, knowledge, model, max_tokens):
    """Build one chunk-level relevance request using the separate prompt file.

    Args:
        question: Nonblank research question.
        requirements: Validated Step 1 information_needed list.
        knowledge: Prepared entity catalogue and complete fact batch.
        model: Anthropic model ID, with no automatic fallback.
        max_tokens: Output token cap for this one request.

    Returns:
        API request with all supplied facts, their entities and evidence. The model
        can return only fact IDs and reasons. Blank questions raise ValueError;
        missing prompt files propagate read errors. No request is sent here.
    """
    if not question.strip():
        raise ValueError("Research question must not be blank")
    schema = {"type": "object", "additionalProperties": False,
              "required": ["selected_facts"], "properties": {"selected_facts": {
                  "type": "array", "items": {"type": "object", "additionalProperties": False,
                  "required": ["fact_id", "reason"], "properties": {
                      "fact_id": {"type": "string"}, "reason": {"type": "string"}}}}}}
    return dict(model=model, max_tokens=max_tokens,
                system=(ROOT / "prompts/select_relevant_evidence.txt").read_text(encoding="utf-8"),
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": json.dumps({
                    "RESEARCH QUESTION": question, "REQUESTED INFORMATION": requirements,
                    "entities": knowledge["entities"], "facts": knowledge["facts"]}, ensure_ascii=False)}])


def validate_selection(text, knowledge):
    """Prevent selections from inventing IDs, duplicating facts or omitting reasons.

    Args:
        text: Exact model response text, without JSON repairs.
        knowledge: Prepared input batch defining the allowed fact IDs.

    Returns:
        Selection object. Raises ValueError for invalid structure, unknown or
        repeated IDs, or blank reasons. Does not validate the relevance judgment.
    """
    selection = Selection.model_validate_json(text)
    allowed = {item["fact_id"] for item in knowledge["facts"]}
    seen = set()
    for item in selection.selected_facts:
        if item.fact_id not in allowed:
            raise ValueError(f"Unknown fact ID: {item.fact_id}")
        if item.fact_id in seen:
            raise ValueError(f"Duplicate selected ID: {item.fact_id}")
        if not item.reason.strip():
            raise ValueError("Selection reason must not be blank")
        seen.add(item.fact_id)
    return selection


def relevant_knowledge(selection, knowledge):
    """Materialize selected originals and their entity context, not model rewrites.

    Args:
        selection: Validated selection referencing the provided knowledge batch.
        knowledge: Original prepared batch with source indices and full fact records.

    Returns:
        Relevant Knowledge content containing exact selected facts, reasons and
        referenced entities, plus unselected IDs. Unknown IDs raise KeyError.
        No files are changed; unselected records remain in the original store.
    """
    by_id = {item["fact_id"]: item for item in knowledge["facts"]}
    selected = [{**by_id[item.fact_id], "reason": item.reason} for item in selection.selected_facts]
    entity_ids = {entry["fact"]["subject_id"] for entry in selected}
    entity_ids.update(entry["fact"]["object_id"] for entry in selected if entry["fact"]["object_id"] is not None)
    selected_ids = {entry["fact_id"] for entry in selected}
    return {"entities": [entity for entity in knowledge["entities"] if entity["id"] in entity_ids],
            "facts": selected, "unselected_fact_ids": [key for key in by_id if key not in selected_ids]}


def main(argv=None):
    """Select evidence from one stored chunk, independently of planning/extraction.

    Args:
        argv: Optional CLI arguments; None uses process arguments. Requires input
            JSON, --question and --requirements. Supports dry-run and size limits.

    Returns:
        Zero for accepted selection (including none), one for model/validation
        failure. Bad inputs and oversized batches exit before calls. Live mode
        writes owner-only receipts and a per-run Relevant Knowledge snapshot;
        failed replies never create that store. Empty knowledge makes zero calls.
        No upstream step, source lookup, retry, input truncation or original-file
        mutation occurs. Successful relevance selection is not claim validation.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("knowledge", type=Path)
    parser.add_argument("--question", required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-input-chars", type=int, default=60000)
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--output-root", type=Path, default=Path.cwd() / "runs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.max_tokens < 1 or args.max_input_chars < 1:
        parser.error("Input and output limits must be positive")
    try:
        raw = args.knowledge.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        source_data = json.loads(raw)
        knowledge = prepare_knowledge(source_data, digest)
        requirements_data = json.loads(args.requirements.read_text(encoding="utf-8"))
        if not isinstance(requirements_data, dict):
            raise ValueError("Requirements must be a JSON object")
        if "status" in requirements_data and requirements_data["status"] not in ("checks_pass", "format_pass"):
            raise ValueError("Cannot use a failed planning receipt")
        requirements_data.pop("credential_redaction_applied", None)
        requirements = validate_plan(json.dumps(requirements_data.get("parsed", requirements_data))).model_dump()["information_needed"]
        request = selection_request(args.question, requirements, knowledge, args.model, args.max_tokens)
        if len(json.dumps(request, ensure_ascii=False)) > args.max_input_chars:
            raise ValueError("Chunk exceeds --max-input-chars; no facts removed and no call made")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    if args.dry_run:
        print(json.dumps({"planned_calls": int(bool(knowledge["facts"])), "request": request}, ensure_ascii=False, indent=2))
        return 0
    key = ""
    if knowledge["facts"]:
        key = (os.environ.get("ANTHROPIC_API_KEY") or dotenv_values(args.env_file).get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            parser.error("Provide ANTHROPIC_API_KEY or --env-file")
    folder = args.output_root / datetime.now(timezone.utc).strftime("relevance-%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, mode=0o700)
    source_ref = {"path": str(args.knowledge.resolve()), "sha256": digest}
    save(folder / "input.json", {"source_artifact": source_ref, "original": source_data}, key)
    receipt = {"request": request, "status": "failed", "attempted_calls": 0,
               "literal_quotes_rechecked": knowledge["literal_quotes_rechecked"]}
    try:
        if knowledge["facts"]:
            with Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=0, timeout=90) as client:
                receipt["attempted_calls"] = 1
                response = client.messages.create(**request)
            receipt["response"] = response.model_dump(mode="json")
            text = "".join(block.text for block in response.content if block.type == "text")
            receipt["raw_text"] = text
            if response.stop_reason != "end_turn":
                raise ValueError(f"Incomplete response: {response.stop_reason}")
        else:
            text = '{"selected_facts": []}'
            receipt["skip_reason"] = "No input facts"
        selection = validate_selection(text, knowledge)
        receipt.update(status="checks_pass", parsed=selection.model_dump())
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        if getattr(error, "response", None) is not None:
            receipt["error"]["body"] = error.response.text
    save(folder / "selection.json", receipt, key)
    if receipt["status"] == "checks_pass":
        store = relevant_knowledge(selection, knowledge)
        save(folder / "relevant_knowledge.json", {"research_question": args.question,
             "requested_information": requirements, "source_artifact": source_ref, **store}, key)
    save(folder / "summary.json", {"status": receipt["status"], "attempted_calls": receipt["attempted_calls"],
         "input_facts": len(knowledge["facts"]), "selected_facts": len(selection.selected_facts) if receipt["status"] == "checks_pass" else None,
         "scope": "Potential relevance only; neither factual truth nor answer completeness is established."}, key)
    print(f"Results: {folder.resolve()}")
    print(f"Selection: {receipt['status']}")
    return int(receipt["status"] != "checks_pass")


if __name__ == "__main__":
    raise SystemExit(main())
