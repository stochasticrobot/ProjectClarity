"""Check the source references supplied on the command line before planning.

This file reads source_urls.txt to obtain web addresses and checks that local file
paths exist. It does NOT download web pages or read document contents. For
example, two paths resolving to the same report receive one source ID.

agentic_workflow.initialize_research() calls load_sources(). Keeping these input
checks here leaves the graph file free to explain the steps and their order.
Actual document reading happens later in the retrieval/ package.
"""

import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from projectclarity.research_schemas import FileSource, Source, URLSource
from projectclarity.research_planner import ResearchError

MAX_SOURCE_FILE_BYTES = 32768
MAX_SOURCES = 25


def load_url_sources(filename: str | None) -> list[URLSource]:
    """Read the URL-list text file, not the pages listed inside it."""
    if filename is None:
        return []
    try:
        with Path(filename).open("rb") as stream:
            content = stream.read(MAX_SOURCE_FILE_BYTES + 1)
        if len(content) > MAX_SOURCE_FILE_BYTES:
            raise ResearchError("The URL list exceeds 32 KiB; use a smaller source list.")
        lines = content.decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise ResearchError("Cannot read the URL list. Check that it is an accessible UTF-8 text file.") from None
    urls = []
    for number, line in enumerate(lines, start=1):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        try:
            parsed = urlsplit(url)
            valid = (
                len(url) <= 2048 and not any(char.isspace() or not char.isprintable() for char in url)
                and parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
            )
            _ = parsed.port  # Accessing this property also rejects malformed ports.
        except ValueError:
            valid = False
        if not valid:
            raise ResearchError(f"Invalid URL on line {number}; use an absolute HTTP(S) URL without embedded credentials.")
        if url not in urls:
            urls.append(url)
        if len(urls) > MAX_SOURCES:
            raise ResearchError("Use at most 25 unique supplied URLs for this planning increment.")
    if not urls:
        raise ResearchError("The supplied URL file has no URLs. Add sources or omit --urls.")
    return [URLSource(source_id=f"S{index}", url=url) for index, url in enumerate(urls, start=1)]


def load_sources(filename: str | None, document_paths: list[str] | None = None) -> list[Source]:
    """Register URLs, then local files, assigning S1, S2, ... within this run.

    A URL-list file is read to obtain addresses. Documents themselves are only
    checked through filesystem metadata; their contents are never opened here.
    Relative document paths start at the current working directory, not beside
    the URL list. Resolving symlinks lets alternate paths to one file share an ID.
    This checks current accessibility, not format support or future readability.
    """
    sources: list[Source] = list(load_url_sources(filename))
    seen_paths: set[Path] = set()
    for number, reference in enumerate(document_paths or [], start=1):
        try:
            if not reference.strip() or "://" in reference or any(not char.isprintable() for char in reference):
                raise ValueError("Expected a local path")
            path = Path(reference).expanduser().resolve(strict=True)
            # Reject directories, FIFOs, devices, and sockets without opening
            # them. os.access is an early check, not a guarantee for later reads.
            if not stat.S_ISREG(path.stat().st_mode) or not os.access(path, os.R_OK):
                raise ValueError("Expected a readable regular file")
            if len(str(path)) > 4096:
                raise ValueError("Resolved path too long")
        except (OSError, ValueError, RuntimeError):
            # Avoid echoing arbitrary filenames/control characters in diagnostics.
            raise ResearchError(f"Document {number} must be an existing, readable local file; check --document paths.") from None
        if path in seen_paths:
            continue
        if len(sources) >= MAX_SOURCES:
            raise ResearchError("Use at most 25 unique supplied sources across URLs and documents.")
        seen_paths.add(path)
        sources.append(FileSource(source_id=f"S{len(sources) + 1}", path=str(path)))
    return sources


