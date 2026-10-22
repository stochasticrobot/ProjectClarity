"""Turn one registered source into saved text plus a provenance receipt.

For example, source S2 might produce documents/S2/raw.bin, text.txt, and
metadata.json. The metadata records its original location, acquisition time,
hashes, parser version, and whether reading/parsing succeeded. Raw bytes remain
available even when their format cannot be parsed. A hash identifies bytes; it
does not prove that their claims are true.

The graph loops over sources and calls retrieve_one(). This module coordinates
reading, parsing, and saving for ONE source; it neither defines LangGraph nodes
nor chooses which node runs next. A bad document produces a failed receipt so
the caller can continue with other documents.
"""

import hashlib
import json
import mimetypes
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from projectclarity.research_schemas import RetrievedDocument, Source
from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError, RunCancelled
from projectclarity.retrieval.document_text_extractor import parse_document
from projectclarity.retrieval.public_url_fetcher import PublicWebReader
from projectclarity.session_storage import write_text
from projectclarity.run_monitor import MonitoringError


def read_local(path: Path, budget: RetrievalBudget) -> bytes:
    """Read the registered path, checking regular-file type, byte caps and in-read changes.

    Registration is not an immutable copy: the file may change before retrieval.
    This check compares size/mtime during this read, not against initialization;
    the receipt fingerprints the bytes actually acquired. A newly substituted
    final symlink is refused, but this is not a full hostile-filesystem sandbox.
    """
    budget.check_time()
    # Recheck at the moment of opening: initialization may have happened before
    # an LLM call. O_NOFOLLOW refuses a newly substituted final symlink and
    # O_NONBLOCK prevents a substituted FIFO from hanging before we can inspect it.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RetrievalError("not_regular_file", "The registered document is no longer a regular file.")
        if before.st_size > budget.limits.max_source_bytes:
            raise RetrievalError("source_size_limit", "The local document exceeds its byte limit.")
        chunks, size = [], 0
        while True:
            chunk = stream.read(min(65536, budget.limits.max_source_bytes - size + 1))
            if not chunk:
                break
            budget.charge_bytes(len(chunk))
            size += len(chunk)
            if size > budget.limits.max_source_bytes:
                raise RetrievalError("source_size_limit", "The local document exceeds its byte limit.")
            chunks.append(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RetrievalError("document_changed", "The document changed while it was being read; rerun with a stable file.")
    return b"".join(chunks)


def retrieve_one(source: Source, run_directory: Path, web: PublicWebReader) -> RetrievedDocument:
    """Acquire and parse one source, keeping completed artifacts and its outcome.

    A parsing failure can still leave useful raw bytes. Failed/cancelled receipts
    let the graph decide whether to continue; failure to create/write the receipt
    itself or loss of monitoring can still raise to the caller. No discovered
    links or new sources are selected here.
    """
    started = time.monotonic()
    record = RetrievedDocument(source_id=source.source_id, source_kind=source.kind,
        original_location=source.url if source.kind == "url" else source.path,
        attempted_at=datetime.now(UTC).isoformat())
    relative = Path("documents") / source.source_id
    directory = run_directory / relative
    directory.mkdir(parents=True, exist_ok=False)
    try:
        web.budget.check_time()
        if source.kind == "url":
            web.budget.emit("source_fetching", stage="fetching")
            reply, record.final_url, record.redirect_chain = web.fetch(source.url)
            raw, record.content_type, record.http_status = reply.body, reply.content_type, reply.status
        else:
            web.budget.emit("source_reading", stage="reading_local")
            raw = read_local(Path(source.path), web.budget)
            record.content_type = mimetypes.guess_type(source.path)[0] or "application/octet-stream"
        record.acquired_at = datetime.now(UTC).isoformat()
        record.byte_count = len(raw)
        record.raw_sha256 = hashlib.sha256(raw).hexdigest()
        web.budget.emit("source_acquired", stage="saving_raw", byte_count=len(raw))
        # Save under a neutral suffix; untrusted HTML/PDF is not served as an
        # executable page. Files are local inspection artifacts, not publications.
        (directory / "raw.bin").write_bytes(raw)
        record.raw_path = str(relative / "raw.bin")
        web.budget.check_time()
        web.budget.emit("source_parsing", stage="parsing", raw_path=record.raw_path)
        inventory = {}
        text, record.parser = parse_document(raw, record.content_type, web.budget.limits,
                                            check_control=web.budget.check_time, link_inventory=inventory)
        web.budget.check_time()
        web.budget.emit("source_saving", stage="saving_text")
        write_text(directory / "text.txt", text)
        record.text_path = str(relative / "text.txt")
        record.character_count = len(text)
        record.text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        record.links = inventory.get("links", []) if source.kind == "url" else []
        record.links_truncated = inventory.get("links_truncated", False)
        record.status = "retrieved"
    except (RunCancelled, KeyboardInterrupt, EOFError):
        # Return a durable cancelled receipt so the graph can retain raw bytes,
        # stop subsequent sources, and distinguish cancellation from failure.
        record.status = "cancelled"
        record.error_code, record.error = "cancelled", "Source acquisition cancelled; completed artifacts retained."
    except MonitoringError:
        raise
    except RetrievalError as exc:
        record.error_code, record.error = exc.code, str(exc)
    except OSError:
        record.error_code, record.error = "file_io", "A source or output file could not be read or written."
    except Exception as exc:
        # Third-party exception messages may echo document content or headers.
        record.error_code, record.error = "retrieval_error", f"Retrieval failed ({type(exc).__name__}); no text accepted."
    record.elapsed_seconds = round(time.monotonic() - started, 3)
    write_text(directory / "metadata.json", record.model_dump_json(indent=2) + "\n")
    return record


def save_manifest(run_directory: Path, records: list[RetrievedDocument]) -> None:
    """Write the one-file index of successful and failed sources after each attempt."""
    write_text(run_directory / "documents.json", json.dumps(
        {"schema_version": "1.0", "documents": [record.model_dump() for record in records]}, indent=2) + "\n")
