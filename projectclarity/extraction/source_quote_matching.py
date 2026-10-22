"""Locate a quotation despite layout whitespace, without changing its words.

HTML links and PDF line wrapping often put a newline where a model writes a
space. We treat runs of whitespace as one space for matching only, then map the
match back to the untouched saved text. The stored quote and character offsets
always describe that original text, not a rewritten document.

Both extraction and claim review use this rule. Changed letters, punctuation,
capitalization, missing words and dehyphenation are NOT repaired. More than one
possible location is rejected even if one occurrence happens to match exactly.
Finding words is a structural check, not proof that they support a claim.
"""
from dataclasses import dataclass
import re


class QuoteMatchError(ValueError):
    """The quotation is absent or has more than one possible source location."""


@dataclass(frozen=True)
class SourceQuoteMatch:
    """A unique original-text slice, relative to the passage passed to the matcher."""

    start: int
    end: int
    quote: str
    match_method: str


def whitespace_index(text: str):
    """Collapse whitespace and retain original start/end offsets for each resulting character.

    A collapsed space maps to the complete whitespace run. Non-whitespace
    characters map one-to-one, including Unicode characters; offsets are Python
    character indices, not UTF-8 byte positions. The original string is untouched.
    """
    characters, starts, ends = [], [], []
    for token in re.finditer(r"\s+|\S+", text):
        if token.group().isspace():
            characters.append(" ")
            starts.append(token.start())
            ends.append(token.end())
        else:
            for position in range(token.start(), token.end()):
                characters.append(text[position])
                starts.append(position)
                ends.append(position + 1)
    return "".join(characters), starts, ends


def match_source_quote(passage: str, submitted_quote: str) -> SourceQuoteMatch:
    """Return one original span or raise an application-owned missing/ambiguous code.

    Outer whitespace in a submitted quote is presentation, not evidence. Interior
    whitespace may differ, but its presence between words cannot be removed or
    invented. Check all normalized occurrences so an exact spelling does not
    conceal a second equally plausible location with different line wrapping.
    """
    needle = " ".join(submitted_quote.split())
    if not needle:
        raise QuoteMatchError("quote_not_found")
    normalized, starts, ends = whitespace_index(passage)
    position = normalized.find(needle)
    if position < 0:
        raise QuoteMatchError("quote_not_found")
    if normalized.find(needle, position + 1) >= 0:
        raise QuoteMatchError("ambiguous_quote")
    start, end = starts[position], ends[position + len(needle) - 1]
    original = passage[start:end]
    return SourceQuoteMatch(start, end, original,
        "exact" if original == submitted_quote else "whitespace_normalized")
