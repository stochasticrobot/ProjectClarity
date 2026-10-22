"""Internal, short-lived document parser; not a command users need to run.

document_text_extractor.py starts this file with raw bytes on stdin. It returns either readable
text or a short failure on stdout. For example, an image-only PDF produces a
no-text result, not invented text. Libraries do the parsing; no PDF JavaScript,
HTML scripts, shell commands, or model instructions are executed.

The worker sets Unix memory/CPU limits before importing parsers. It has a clean
environment and no source path, but it is not a complete security sandbox.
HTML cleanup is deliberately lossy: headers/navigation can contain useful names
or links that will not reach the text. Raw bytes are retained by retrieval, but
a bounded href/label inventory is collected before cleanup, separately from evidence text. PDF text has no OCR or
layout guarantee. Later quotations refer to normalized text, not page coordinates.
"""

import io
import json
import re
import sys
import unicodedata


def extract(raw: bytes, media_type: str, charset: str, max_chars: int, max_pages: int) -> tuple[str, str]:
    """Convert one supported document into normalized text and a parser-version label.

    Format libraries extract text; the final cleanup standardizes whitespace and
    Unicode for exact quotation matching later. Limits reject excessive/empty
    output rather than returning a silently truncated document. HTML links are
    reduced to visible text here; html_links() separately captures observed targets.
    """
    if media_type == "application/pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf")
        if len(reader.pages) > max_pages:
            raise ValueError("page_limit")
        pages = []
        count = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            count += len(text)
            if count > max_chars:
                raise ValueError("text_size_limit")
            pages.append(text)
        text, parser = "\n\n".join(pages), "pypdf-" + pypdf.__version__
    elif media_type in {"text/html", "application/xhtml+xml"}:
        import bs4
        soup = bs4.BeautifulSoup(raw, "html.parser", from_encoding=charset or None)
        # These sections usually contain controls or executable markup, not the
        # article. This heuristic is intentionally simple; raw HTML is also saved.
        for element in soup.find_all(["script", "style", "nav", "header", "footer", "form", "noscript", "template", "svg", "iframe"]):
            element.decompose()
        body = soup.find("main") or soup.find("article") or soup.body or soup
        text, parser = body.get_text("\n", strip=True), "beautifulsoup4-" + bs4.__version__ + "/html.parser"
    else:
        encoding = charset or ("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        text, parser = raw.decode(encoding), "python-text-v1"
    if len(text) > max_chars:
        raise ValueError("text_size_limit")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if char.isprintable() or char in "\n\t")
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("no_text")
    if len(text) + 1 > max_chars:
        raise ValueError("text_size_limit")
    return text + "\n", parser + "/normalize-v1"


def html_links(raw: bytes, media_type: str, charset: str) -> dict:
    """Retain up to 100 observed anchors before text cleanup removes navigation.

    Targets are raw attributes, not permissions. The parent resolves them against
    the final fetched URL; HTML base tags are deliberately ignored. PDF/text link
    discovery is not implemented. Labels are untrusted hints, not source claims.
    """
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return {"links": [], "links_truncated": False}
    import bs4
    soup = bs4.BeautifulSoup(raw, "html.parser", from_encoding=charset or None)
    anchors = soup.find_all("a", href=True, limit=101)
    return {"links": [{"href": str(a["href"])[:2048], "label": a.get_text(" ", strip=True)[:240]}
        for a in anchors[:100] if len(str(a["href"])) <= 2048], "links_truncated": len(anchors) > 100}


def main() -> None:
    """Apply resource limits, parse one input, and never print raw exception details."""
    errors = {"encrypted_pdf": "Encrypted PDFs are not supported.",
              "page_limit": "The PDF exceeds the page limit.",
              "text_size_limit": "Extracted text exceeds the character limit.",
              "no_text": "No readable text found; scanned PDFs need OCR, which is not implemented."}
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 11))
        raw = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("input_limit")
        text, parser = extract(raw, sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
        payload = {"text": text, "parser": parser, **html_links(raw, sys.argv[1], sys.argv[2])}
    except (ImportError, OSError):
        payload = {"error_code": "parser_setup", "error": "Parser dependencies or Unix resource limits are unavailable."}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in errors else "parse_error"
        payload = {"error_code": code, "error": errors.get(code, "Cannot extract readable text from this document.")}
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
