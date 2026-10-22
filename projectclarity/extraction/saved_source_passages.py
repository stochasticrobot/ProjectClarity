"""Read the exact saved source version and divide it into bounded pieces.

The model cannot read our disk. This module reads documents/S1/text.txt after
retrieval, verifies its recorded SHA-256 fingerprint, and supplies one piece at
a time. Offsets refer to that normalized text, not HTML bytes or PDF pages.
Both extraction and evidence validation use the same reader so edited text
cannot quietly become the evidence for an earlier model response.

Passages are non-overlapping, at most 12,000 characters, preferring newline
boundaries. Long paragraphs can be split; this simple first version does not
solve cross-passage reasoning. No text is silently truncated or ranked away:
unattempted passages remain visible in the extraction receipts.
This coverage guarantee applies to saved normalized text, not everything in the
original HTML/PDF: retrieval's parser may have removed navigation or lost layout.
"""

import hashlib
from pathlib import Path


MAX_PASSAGE_CHARACTERS = 12_000
MAX_SAVED_TEXT_BYTES = 8 * 1024 * 1024


class EvidenceInputError(ValueError):
    """Carry a fixed diagnostic code, never a private path or document body."""


def read_verified_text(directory: Path, document) -> str:
    """Read bounded UTF-8 text only if its saved path and fingerprint still match.

    The path must resolve inside this run's documents directory; the final file
    cannot be a symlink. Moving an intact run is allowed. This verifies the
    normalized evidence version, not the truth of statements or raw-page layout.
    """
    try:
        root = directory.resolve()
        relative = Path(document.text_path or "")
        path = root / relative
        if (relative.is_absolute() or path.is_symlink()
                or not path.resolve().is_relative_to(root / "documents") or not path.is_file()):
            raise EvidenceInputError("invalid_text_path")
        with path.open("rb") as stream:
            body = stream.read(MAX_SAVED_TEXT_BYTES + 1)
        if len(body) > MAX_SAVED_TEXT_BYTES:
            raise EvidenceInputError("saved_text_too_large")
        if hashlib.sha256(body).hexdigest() != document.text_sha256:
            raise EvidenceInputError("source_version_mismatch")
        return body.decode("utf-8")
    except (OSError, UnicodeError):
        raise EvidenceInputError("saved_text_unreadable") from None


def iter_passages(text: str):
    """Yield stable passage IDs and exact offsets covering all the supplied text."""
    start, number = 0, 1
    while start < len(text):
        end = min(len(text), start + MAX_PASSAGE_CHARACTERS)
        if end < len(text):
            boundary = text.rfind("\n", start + MAX_PASSAGE_CHARACTERS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield f"P{number}", start, end
        start, number = end, number + 1
