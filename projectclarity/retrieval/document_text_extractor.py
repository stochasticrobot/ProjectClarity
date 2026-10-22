"""Ask a bounded helper process to turn document bytes into readable text.

The retrieval pipeline calls parse_document() after obtaining a complete file.
For example, PDF bytes become extracted page text, while HTML loses navigation
and scripts. The separate _document_text_worker.py process limits memory/CPU use: a bad
PDF must not be allowed to consume the parent research process indefinitely.
No LLM is used, and the helper receives neither credentials nor a source path.
This is process/resource isolation, not an operating-system security sandbox.
The parent polls while waiting so an operator stop request can kill and reap the
helper. Parser cancellation is distinct from malformed or unsupported documents.
"""

import json
import os
import subprocess
import sys
import time
from email.message import Message
from pathlib import Path

from projectclarity.retrieval.retrieval_limits import RetrievalError, RetrievalLimits


def parse_document(raw: bytes, content_type: str, limits: RetrievalLimits, *, check_control=None, link_inventory=None) -> tuple[str, str]:
    """Return text/parser label; optionally fill a bounded observed-anchor inventory.

    Link capture adds metadata, not network calls. Existing callers can ignore
    it and keep the two-value text interface. Unsupported/failed parsing raises.
    """
    header = Message()
    header["content-type"] = content_type
    media_type = header.get_content_type()
    if raw.startswith(b"%PDF-"):
        media_type = "application/pdf"
    if media_type not in {"text/plain", "text/markdown", "text/html", "application/xhtml+xml", "application/pdf"}:
        raise RetrievalError("unsupported_format", "Supported formats are text, Markdown, HTML, and text-based PDF.")
    command = [sys.executable, "-I", str(Path(__file__).with_name("_document_text_worker.py")),
               media_type, header.get_content_charset() or "", str(limits.max_text_characters),
               str(limits.max_pdf_pages)]
    # Popen + short communicate timeouts lets a stop request terminate a parser,
    # not just wait for its full hard timeout. communicate safely drains stdout
    # while sending input; retrying it without input continues the same process.
    # The minimal environment and -I prevent inherited model keys/customizations.
    deadline = time.monotonic() + limits.parser_timeout_seconds
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env={"PATH": os.defpath, "LANG": "C.UTF-8"}) as process:
        first_attempt = True
        try:
            while True:
                if check_control:
                    check_control()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RetrievalError("parser_timeout", "Text parsing exceeded its time allowance.")
                try:
                    output, _ = process.communicate(input=raw if first_attempt else None, timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    first_attempt = False
        except BaseException:
            # Reap the child for cancellation, Ctrl-C, deadline, and unexpected
            # errors; no orphan PDF parser should survive its research worker.
            process.kill()
            process.communicate()
            raise
    if process.returncode != 0:
        raise RetrievalError("parser_failed", "Text parsing failed or exceeded its process resource limits.")
    try:
        payload = json.loads(output)
    except (ValueError, UnicodeError):
        raise RetrievalError("parser_failed", "The text parser did not return a usable result.") from None
    if "error_code" in payload:
        raise RetrievalError(payload["error_code"], payload["error"])
    if link_inventory is not None:
        link_inventory.update(links=payload.get("links", []), links_truncated=payload.get("links_truncated", False))
    return payload["text"], payload["parser"]
