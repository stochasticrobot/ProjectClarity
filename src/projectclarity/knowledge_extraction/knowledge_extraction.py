"""Define how information from a document becomes a saved fact with source evidence.

The application needs facts it can later select, review and cite, rather than an
unstructured AI summary. This file defines the expected information about people,
organizations and other subjects, and about the assertions made about them. It
builds extraction requests and checks replies for the required fields, consistent
subject references and quotations found in the supplied text.

The web workflow uses these shared rules. This file also provides a command-line
way to extract information from text files on its own. Its strict checks reject a
supplied group if any fact is invalid; fact_validation_and_repair.py lets the web workflow
check facts separately so valid neighbours can survive. A matching quotation does
not by itself prove that a claim is true or that the quote supports every detail.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from anthropic import Anthropic
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Record(BaseModel):
    """Reject unknown fields in extraction records."""
    model_config = ConfigDict(extra="forbid", strict=True)


class Entity(Record):
    """A chunk-local entity, including events, with a general type and specific type."""
    id: str
    name: str
    type: str
    subtype: str | None


class Fact(Record):
    """A source statement linking an entity to a literal value or another entity.

    statement retains the full meaning, including amounts, dates, negation and
    uncertainty. entity IDs are local to the current chunk, not global identities.
    """
    subject_id: str
    predicate: str
    value: str
    object_id: str | None
    statement: str
    quote: str


class Extraction(Record):
    """Typed entities and source assertions, not independently validated claims."""
    entities: list[Entity]
    facts: list[Fact]


def wire_schema():
    """Build the API response structure without extra model-facing descriptions.

    Args:
        None.

    Returns:
        A fresh JSON schema with structural constraints only; no I/O occurs.
    """
    schema = Extraction.model_json_schema()
    for definition in [schema, *schema.get("$defs", {}).values()]:
        definition.pop("description", None)
        definition.pop("title", None)
    return schema


def extraction_request(chunk, requirements, model, max_tokens):
    """Build an extraction request with optional Task 1 information as data.

    Args:
        chunk: Exact source text for one extraction call.
        requirements: Validated information_needed list; empty for generic extraction.
        model: Anthropic model identifier.
        max_tokens: Per-call output allowance, not a completeness guarantee.

    Returns:
        API request with the editable extraction prompt loaded unchanged, the
        existing schema and a JSON data message. No call or prompt suffix is added.
    """
    return dict(model=model, max_tokens=max_tokens,
                system=(ROOT / "prompts/structured_information_extraction.txt").read_text(encoding="utf-8"),
                output_config={"format": {"type": "json_schema", "schema": wire_schema()}},
                messages=[{"role": "user", "content": json.dumps({
                    "requested_information": requirements, "document_chunk": chunk}, ensure_ascii=False)}])



def validate_response(text, chunk):
    """Check parseability, local references and literal evidence before reporting success.

    Args:
        text: Exact model response text, with no fence stripping or repair.
        chunk: Exact source text sent to the model.

    Returns:
        Validated extraction. Raises ValueError on missing or duplicate entity IDs,
        dangling references, blank fields or quotes absent from the chunk. These
        checks do not establish factual entailment or extraction completeness.
    """
    result = Extraction.model_validate_json(text)
    ids = {entity.id for entity in result.entities}
    if len(ids) != len(result.entities):
        raise ValueError("Duplicate entity IDs")
    for entity in result.entities:
        if not all(value.strip() for value in (entity.id, entity.name, entity.type)):
            raise ValueError("Blank entity field")
    for fact in result.facts:
        if fact.subject_id not in ids or (fact.object_id is not None and fact.object_id not in ids):
            raise ValueError("Dangling entity reference")
        if not all(value.strip() for value in (fact.predicate, fact.value, fact.statement, fact.quote)):
            raise ValueError("Blank fact field")
        if fact.quote not in chunk:
            raise ValueError("Supporting quote not found in chunk")
    return result


def save(path, data, key):
    """Persist private evidence while removing credentials from error messages.

    Args:
        path: New JSON artifact path under the run directory.
        data: JSON-serializable receipt or summary.
        key: Exact provider secret to redact in addition to recognizable key patterns.

    Returns:
        None. Creates an owner-only JSON file; write failures propagate. Redaction
        is recorded explicitly and original provider errors otherwise stay intact.
    """
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    safe = raw.replace(key, "[REDACTED]") if key else raw
    safe = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "[REDACTED]", safe)
    value = json.loads(safe)
    value["credential_redaction_applied"] = safe != raw
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def main():
    """Extract from UTF-8 text with explicit chunk boundaries and a bounded call count.

    Args:
        None. CLI takes a text path, optional Step 1 JSON, --chunk-chars (zero means
        whole document), --max-calls, --model, --env-file and --dry-run.

    Returns:
        Zero when every selected chunk passes structural/evidence checks, one on
        a failed call. Dry-run prints complete requests without network access.
        Oversized call counts fail before any API call. Live calls have no retries
        or fallback, retain raw responses/errors, and write owner-only receipts.
        Character slicing may split sentences; no cross-chunk merging is attempted.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--chunk-chars", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--env-file", type=Path, default=Path.cwd() / ".env")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.chunk_chars < 0 or args.max_calls < 1:
        parser.error("Chunk size must be nonnegative and max calls positive")
    document = args.document.read_text(encoding="utf-8")
    step1 = json.loads(args.requirements.read_text()) if args.requirements else {}
    requested = step1.get("parsed", step1).get("information_needed", [])
    if not isinstance(requested, list):
        parser.error("information_needed must be a list")
    size = args.chunk_chars or max(1, len(document))
    chunks = [(start, document[start:start + size]) for start in range(0, len(document), size)] or [(0, "")]
    if len(chunks) > args.max_calls:
        parser.error(f"Needs {len(chunks)} calls, above --max-calls; no calls made")
    prompt = (ROOT / "prompts/structured_information_extraction.txt").read_text()
    requests = [dict(model=args.model, max_tokens=8192, system=prompt,
                     output_config={"format": {"type": "json_schema", "schema": wire_schema()}},
                     messages=[{"role": "user", "content": json.dumps({
                         "requested_information": requested, "document_chunk": chunk}, ensure_ascii=False)}])
                for _, chunk in chunks]
    if args.dry_run:
        print(json.dumps(requests, ensure_ascii=False, indent=2))
        return 0
    key = (os.environ.get("ANTHROPIC_API_KEY") or dotenv_values(args.env_file).get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        parser.error("Provide ANTHROPIC_API_KEY or --env-file")
    folder = Path.cwd() / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, mode=0o700)
    print(f"Results: {folder}", flush=True)
    results = []
    with Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=0, timeout=90) as client:
        for index, ((start, chunk), request) in enumerate(zip(chunks, requests)):
            receipt = {"source": str(args.document.resolve()), "source_sha256": hashlib.sha256(document.encode()).hexdigest(),
                       "chunk_index": index, "start_char": start, "end_char": start + len(chunk), "request": request}
            try:
                response = client.messages.create(**request)
                receipt["response"] = response.model_dump(mode="json")
                text = "".join(block.text for block in response.content if block.type == "text")
                receipt["raw_text"] = text
                if response.stop_reason != "end_turn":
                    raise ValueError(f"Incomplete response: {response.stop_reason}")
                result = validate_response(text, chunk)
                receipt.update(parsed=result.model_dump(), status="checks_pass")
            except Exception as error:
                receipt.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
                if getattr(error, "response", None) is not None:
                    receipt["error"]["body"] = error.response.text
            save(folder / f"chunk-{index:04d}.json", receipt, key)
            results.append(receipt["status"])
            print(f"Chunk {index}: {receipt['status']}", flush=True)
    save(folder / "summary.json", {"results": results, "scope": "Structure, references and literal quotes only; not accuracy or completeness."}, key)
    return int("failed" in results)


if __name__ == "__main__":
    raise SystemExit(main())
