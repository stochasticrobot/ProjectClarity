"""Read the exact saved source version and divide it into bounded pieces.

The model cannot read our disk. This module reads documents/S1/text.txt after
retrieval, verifies its recorded SHA-256 fingerprint, and supplies one piece at
a time. Offsets refer to that normalized text, not HTML bytes or PDF pages.
Both extraction and evidence validation use the same reader so edited text
cannot quietly become the evidence for an earlier model response.

Passages are non-overlapping, at most 12,000 characters. Parser-labelled HTML
records stay intact within that ceiling and use smaller groups; ordinary prose
prefers newline boundaries. Oversized records and long paragraphs can still split.
No text is silently truncated or ranked away:
unattempted passages remain visible in the extraction receipts.
This coverage guarantee applies to saved normalized text, not everything in the
original HTML/PDF: retrieval's parser may have removed navigation or lost layout.
"""

import hashlib
import re
from bisect import bisect_left, bisect_right
from pathlib import Path


MAX_PASSAGE_CHARACTERS = 12_000
MAX_SAVED_TEXT_BYTES = 8 * 1024 * 1024
MAX_RECORDS_PER_PASSAGE = 8
RECORD_PASSAGE_CHARACTERS = 3000
RECORD_START = re.compile(r'^\[Record (?:T\d+R\d+|C\d+)\]\n', re.MULTILINE)


class EvidenceInputError(ValueError):
    """Carry a fixed diagnostic code, never a private path or document body."""


def read_verified_text(directory: Path, document) -> str:
    """Read bounded UTF-8 text only if its saved path and fingerprint still match.

    The path must resolve inside this run's documents directory; the final file cannot be a
    symlink. Moving an intact run is allowed. This verifies the normalized evidence version,
    not the truth of statements or raw-page layout.

    Args:
        directory: Local filesystem directory for this one saved research run.
        document: RetrievedDocument receipt containing the expected normalized-text path and
            fingerprint.

    Returns:
        Decoded normalized source text whose path, size and SHA-256 match the saved receipt;
        otherwise raise EvidenceInputError.
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
    """Yield stable passage IDs and exact offsets covering all the supplied text.

    Args:
        text: Complete normalized source text to divide into bounded extraction passages.

    Returns:
        Successive (passage ID, start, end) tuples covering every character once.
        Parser-labelled records are grouped up to eight at a time, aiming for 3,000
        characters; a single larger record stays intact up to the hard 12,000 cap.
        This is input accounting, not a claim that a model extracted every fact.
    """
    # Record boundaries are structural formatting, never instructions or evidence
    # of relevance. Old saved inventories retain their original offsets on restart.
    records = list(RECORD_START.finditer(text))
    record_starts = [match.start() for match in records]
    boundaries = {match.start() for match in records}
    for match in records:
        end = text.find('\n\n', match.end())
        boundaries.add(len(text) if end < 0 else end + 2)
    boundaries = sorted(boundaries)
    start, number = 0, 1
    while start < len(text):
        end = min(len(text), start + MAX_PASSAGE_CHARACTERS)
        upcoming = records[bisect_left(record_starts, start):bisect_left(record_starts, end)]
        if upcoming:
            if upcoming[0].start() > start:
                # Do not pack a large prose prefix with the first dense record.
                end = upcoming[0].start()
            else:
                target = min(end, start + RECORD_PASSAGE_CHARACTERS,
                             upcoming[MAX_RECORDS_PER_PASSAGE].start() if len(upcoming) > MAX_RECORDS_PER_PASSAGE else end)
                first = bisect_right(boundaries, start)
                after = bisect_right(boundaries, target)
                if after > first:
                    end = boundaries[after - 1]
                else:
                    end = min(end, boundaries[first]) if first < len(boundaries) else end
        elif end < len(text):
            boundary = text.rfind("\n", start + MAX_PASSAGE_CHARACTERS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        yield f"P{number}", start, end
        start, number = end, number + 1
