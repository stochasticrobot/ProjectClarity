"""Try requirements definition and knowledge extraction without running the web app.

Developers need to inspect these steps separately when changing prompts or testing
new documents. This command-line tool can define the answer requirements, extract
information from supplied text files, or run both in sequence so the extraction
receives the requirements produced for that question.

It saves what was sent to the AI and what came back so the handoff can be reviewed.
A preview option shows the planned work without sending requests. This tool uses
the strict extraction checks; it does not run the web workflow's extra fact-repair
step, web searches, claim review or report generation.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import dotenv_values

from projectclarity.knowledge_extraction.knowledge_extraction import save, validate_response, extraction_request
from projectclarity.question_definition.requirements_definition import planning_request, validate_plan

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "claude-haiku-5-5"


def prepare_chunks(paths, chunk_chars):
    """Read UTF-8 documents and preserve exact text with document-local offsets.

    Args:
        paths: Input document paths; an empty file still yields one empty chunk.
        chunk_chars: Maximum characters per chunk, or zero for whole documents.

    Returns:
        Ordered chunk dictionaries containing source path/hash, index, offsets and
        text. Negative sizes raise ValueError; read errors propagate before calls.
        No overlap, parsing, context insertion or cross-document merging occurs.
    """
    if chunk_chars < 0:
        raise ValueError("Chunk size must be nonnegative")
    chunks = []
    for path in paths:
        document = path.read_text(encoding="utf-8")
        size = chunk_chars or max(1, len(document))
        starts = list(range(0, len(document), size)) or [0]
        for index, start in enumerate(starts):
            text = document[start:start + size]
            chunks.append(dict(source=str(path.resolve()), source_sha256=hashlib.sha256(document.encode()).hexdigest(),
                               chunk_index=index, start_char=start, end_char=start + len(text), text=text))
    return chunks




def call_step(client, request, chunk=None):
    """Call and validate one step while retaining complete provider evidence.

    Args:
        client: Anthropic client configured with retries disabled.
        request: Exact request dictionary to send once.
        chunk: Source text for extraction validation, or None for planner validation.

    Returns:
        Receipt with request, raw response/text and parsed data on success. Errors
        retain their message/body and return status=failed without retry. A valid
        extraction's status covers structure/references/quotes, not factual truth.
        Caller must save this through the credential-redacting private writer.
    """
    receipt = {"request": request, "status": "failed"}
    try:
        response = client.messages.create(**request)
        receipt["response"] = response.model_dump(mode="json")
        text = "".join(block.text for block in response.content if block.type == "text")
        receipt["raw_text"] = text
        if response.stop_reason != "end_turn":
            raise ValueError(f"Incomplete response: {response.stop_reason}")
        parsed = validate_plan(text) if chunk is None else validate_response(text, chunk)
        receipt.update(parsed=parsed.model_dump(), status="checks_pass")
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        if getattr(error, "response", None) is not None:
            receipt["error"]["body"] = error.response.text
    return receipt


def write_report(folder):
    """Make an inspectable report from already-redacted private receipts.

    Args:
        folder: Completed or planner-failed run directory containing summary.json.

    Returns:
        None. Creates an owner-only report.md; I/O failures propagate. Includes
        requirement and extraction outputs, without promoting checks to accuracy.
    """
    parts = ["# Test rig results\n\nChecks cover format, references and quote presence—not factual correctness or completeness.\n"]
    files = [folder / "summary.json", folder / "plan.json", *sorted(folder.glob("chunk-*.json"))]
    for path in files:
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        shown = data if path.name == "summary.json" else {key: data[key] for key in ("status", "source", "chunk_index", "parsed", "error") if key in data}
        parts.append(f"\n## {path.name}\n\n```json\n{json.dumps(shown, ensure_ascii=False, indent=2)}\n```\n")
    path = folder / "report.md"
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write("".join(parts))


def main(argv=None):
    """Run a bounded standalone or two-step experiment with inspectable artifacts.

    Args:
        argv: Optional CLI argument list for tests; None reads process arguments.

    Returns:
        Zero if all selected steps pass checks, one for provider/validation failure.
        Invalid inputs/budgets exit through argparse before calls. Dry-run makes no
        calls and reads no credentials. A failed planner stops combined mode before
        extraction. Live mode creates a private run folder; no automatic retries,
        resume, search, cross-chunk identity resolution or claim validation occurs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "extract", "combined"])
    parser.add_argument("documents", nargs="*", type=Path)
    parser.add_argument("--question")
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--chunk-chars", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=8, help="Total calls, including planning")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--plan-max-tokens", type=int, default=4096)
    parser.add_argument("--extract-max-tokens", type=int, default=8192)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    needs_plan = args.mode in ("plan", "combined")
    if needs_plan and (not args.question or not args.question.strip()):
        parser.error("plan and combined modes require --question")
    if args.mode == "extract" and args.question is not None:
        parser.error("Use combined mode to turn a question into requirements")
    if args.mode == "plan" and args.documents:
        parser.error("plan mode takes a question, not documents")
    if args.mode != "plan" and not args.documents:
        parser.error("extract and combined modes require document paths")
    if args.requirements and args.mode != "extract":
        parser.error("--requirements is for extract mode; combined mode generates them")
    if min(args.max_calls, args.plan_max_tokens, args.extract_max_tokens) < 1:
        parser.error("Call and output-token limits must be positive")
    try:
        chunks = prepare_chunks(args.documents, args.chunk_chars)
        requirements = []
        if args.requirements:
            saved = json.loads(args.requirements.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("Requirements must be a JSON object")
            saved.pop("credential_redaction_applied", None)
            if "status" in saved and saved["status"] not in ("checks_pass", "format_pass"):
                raise ValueError("Cannot reuse a failed planning receipt")
            requirements = validate_plan(json.dumps(saved.get("parsed", saved))).model_dump()["information_needed"]
        plan_request = planning_request(args.question, args.model, args.plan_max_tokens) if needs_plan else None
        # Read the extraction prompt even in combined mode before any paid call.
        preview = [extraction_request(c["text"], requirements, args.model, args.extract_max_tokens) for c in chunks]
    except (OSError, ValueError) as error:
        parser.error(str(error))
    call_count = len(chunks) + int(needs_plan)
    if call_count > args.max_calls:
        parser.error(f"Needs {call_count} calls, above --max-calls; no calls made")
    if args.dry_run:
        print(json.dumps({"mode": args.mode, "planned_calls": call_count, "planner_request": plan_request,
                          "extraction_requests": preview if not needs_plan else [],
                          "pending_extraction_chunks": chunks if needs_plan else [],
                          "note": "Combined extraction requests are built only after a valid planner response."}, ensure_ascii=False, indent=2))
        return 0
    key = (os.environ.get("ANTHROPIC_API_KEY") or dotenv_values(args.env_file).get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        parser.error("Provide ANTHROPIC_API_KEY or --env-file")
    folder = args.output_root / datetime.now(timezone.utc).strftime("rig-%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, mode=0o700)
    save(folder / "manifest.json", {"mode": args.mode, "question": args.question, "planned_calls": call_count,
         "model": args.model, "chunks": chunks, "requirements_source": str(args.requirements) if args.requirements else None}, key)
    print(f"Results: {folder.resolve()}", flush=True)
    statuses = []
    with Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=0, timeout=90) as client:
        if needs_plan:
            receipt = call_step(client, plan_request)
            save(folder / "plan.json", receipt, key)
            statuses.append({"step": "plan", "status": receipt["status"]})
            print(f"Plan: {receipt['status']}", flush=True)
            if receipt["status"] == "checks_pass":
                requirements = receipt["parsed"]["information_needed"]
        if not needs_plan or statuses[0]["status"] == "checks_pass":
            save(folder / "requirements.json", {"information_needed": requirements}, key)
            for index, chunk in enumerate(chunks):
                request = extraction_request(chunk["text"], requirements, args.model, args.extract_max_tokens)
                receipt = call_step(client, request, chunk["text"])
                receipt.update({k: v for k, v in chunk.items() if k != "text"})
                save(folder / f"chunk-{index:04d}.json", receipt, key)
                statuses.append({"step": f"chunk-{index:04d}", "status": receipt["status"]})
                print(f"Chunk {index}: {receipt['status']}", flush=True)
    failed = any(s["status"] != "checks_pass" for s in statuses)
    save(folder / "summary.json", {"mode": args.mode, "question": args.question, "planned_calls": call_count,
         "attempted_calls": len(statuses), "status": "failed" if failed else "checks_pass", "steps": statuses,
         "scope": "Format/reference/quote checks, not semantic accuracy or research completeness."}, key)
    write_report(folder)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
