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


def table_span(value: str | None, limit: int, *, allow_zero: bool = False) -> int:
    """Recover HTML integer prefixes without permitting unbounded grid expansion.

    HTML's non-negative integer parsing accepts leading ASCII whitespace, a sign
    and decimal digits, ignoring trailing junk (e.g. ``2 style=`` means two).
    Missing/invalid/negative values default to one, as table processing requires.
    See https://html.spec.whatwg.org/multipage/common-microsyntaxes.html#rules-for-parsing-non-negative-integers
    Application resource ceilings remain stricter than browser layout limits.

    Args:
        value: Raw rowspan/colspan attribute, or None when absent.
        limit: Maximum accepted positive span for this bounded table grid.
        allow_zero: Preserve zero for rowspan's downward-growth handling; colspan
            zero instead defaults to one.

    Returns:
        Recovered span or one for an invalid attribute. Oversized positive spans
        raise ValueError before integer conversion or grid allocation; no clipping
        or field-shifting fallback is performed.
    """
    match = re.match(r'[\t\n\f\r ]*([+-]?)([0-9]+)', value or '')
    if match is None:
        return 1
    digits = match.group(2).lstrip('0') or '0'
    if match.group(1) == '-' and digits != '0':
        return 1
    # Comparing digit strings avoids Python's huge-integer conversion limit and
    # bounds allocation even when a malformed page supplies thousands of digits.
    ceiling = str(limit)
    if len(digits) > len(ceiling) or (len(digits) == len(ceiling) and digits > ceiling):
        raise ValueError('table_span_limit')
    span = int(digits)
    return span if span or allow_zero else 1


def table_records(table, number: int, max_chars: int, context: str = '') -> str:
    """Render labelled table records, expanding shared cells without guessing facts.

    Args:
        table: BeautifulSoup table belonging to the cleaned source DOM.
        number: Document-local table number used only for structural record IDs.
        max_chars: Output ceiling; excessive expansion raises ValueError.
        context: Exact enclosing source-heading text, or empty when unavailable.

    Returns:
        Text with repeated headers and complete rowspan/colspan values per row.
        Nested tables remain cell text. Recoverable span syntax is normalized;
        overlapping or oversized grids still fail explicitly. Source headings
        repeat inside each record so independently processed passages retain scope.
    """
    rows = [row for row in table.find_all('tr') if row.find_parent('table') is table]
    grid, headers, output = {}, {}, []
    size = 0
    caption = table.find('caption', recursive=False)
    if caption:
        output.append(caption.get_text(' ', strip=True))
    for index, row in enumerate(rows):
        cells = row.find_all(['th', 'td'], recursive=False)
        column = 0
        for cell in cells:
            while (index, column) in grid:
                column += 1
            height = table_span(cell.get('rowspan'), len(rows), allow_zero=True)
            height = len(rows) - index if height == 0 else height
            width = table_span(cell.get('colspan'), 256)
            if not 1 <= height <= len(rows) or not 1 <= width <= 256 or column + width > 256:
                raise ValueError('table_span_limit')
            value = cell.get_text(' ', strip=True)
            for dy in range(min(height, len(rows) - index)):
                for dx in range(width):
                    position = (index + dy, column + dx)
                    if position in grid:
                        raise ValueError('overlapping_table_cells')
                    grid[position] = value
                    size += len(value) + 1
                    if size > max_chars * 4:
                        raise ValueError('text_size_limit')
            column += width
        values = {col: value for (row_index, col), value in grid.items() if row_index == index}
        # Mixed TH/TD rows are data, including row headers, not column headers.
        if cells and all(cell.name == 'th' for cell in cells):
            for col, value in values.items():
                if value:
                    headers[col] = value
            output.append(' | '.join(value for _, value in sorted(values.items()) if value))
        elif values:
            fields = [f'{headers[col]}: {value}' if headers.get(col) else value
                      for col, value in sorted(values.items()) if value]
            output.append(f'[Record T{number}R{index + 1}]\n' + context + ' | '.join(fields))
        for position in [position for position in grid if position[0] == index]:
            del grid[position]
    result = '\n\n'.join(output)
    if len(result) > max_chars:
        raise ValueError('text_size_limit')
    return result


def record_contexts(body, targets, title=''):
    """Capture document heading scope before replacing tables and cards with text.

    Args:
        body: Cleaned content DOM in its original source order.
        targets: Table/card elements that will become independent extraction records.
        title: Actual HTML document title, or empty when none was supplied.
    Returns:
        Element-ID mapping to source-title/heading prefixes. Standard h1–h6 nesting
        replaces equal/deeper headings; headings inside records do not leak into
        siblings. Only actual title/heading text is copied, never an inferred date,
        authority, geography or factual conclusion. No DOM mutation occurs.
    """
    ids = {id(target) for target in targets}
    stack, contexts = {}, {}
    for element in body.descendants:
        name = getattr(element, 'name', None)
        if id(element) in ids:
            headings = [stack[level] for level in sorted(stack)]
            contexts[id(element)] = (('Document title: ' + title + '\n') if title else '') + (
                'Source headings: ' + ' > '.join(headings) + '\n' if headings else '')
        if name in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} and not any(id(p) in ids for p in element.parents):
            level = int(name[1])
            stack = {depth: title for depth, title in stack.items() if depth < level}
            heading_title = element.get_text(' ', strip=True)
            if heading_title:
                stack[level] = heading_title
    return contexts


def structured_html_text(body, max_chars: int, title: str = '') -> str:
    """Preserve table and repeated-card boundaries without site-specific rules.

    Args:
        body: Cleaned BeautifulSoup content element, mutated only in this worker.
        max_chars: Maximum output size, also bounding table expansion.
        title: Actual document TITLE text, preserved as untrusted source context.

    Returns:
        Visible text with structural record labels, not inferred facts. DIV/LI cards
        are detected by sibling shape, never URLs, names or benchmark answers.
        Actual enclosing headings repeat within each record to preserve source
        scope across passage boundaries; they remain untrusted source content.
    """
    from collections import defaultdict
    from bs4 import NavigableString

    cards = []
    for parent in [body, *body.find_all(['div', 'ul', 'ol'])]:
        groups = defaultdict(list)
        for child in parent.find_all(['div', 'li'], recursive=False):
            groups[(child.name, tuple(child.get('class', [])))].append(child)
        for siblings in groups.values():
            if len(siblings) < 3:
                continue
            for child in siblings:
                if child.find('table') or child.find_parent('table'):
                    continue
                pieces = list(child.stripped_strings)
                if 2 <= len(pieces) <= 32 and sum(map(len, pieces)) <= 2000:
                    cards.append(child)
    selected = {id(card) for card in cards}
    tables = [table for table in body.find_all('table') if not table.find_parent('table')]
    contexts = record_contexts(body, [*cards, *tables], title)
    for number, card in enumerate(cards, 1):
        if any(id(parent) in selected for parent in card.parents):
            continue
        value = ' | '.join(card.stripped_strings)
        card.replace_with(NavigableString(f'\n\n[Record C{number}]\n{contexts[id(card)]}{value}\n\n'))
    for number, table in enumerate(tables, 1):
        table.replace_with(NavigableString('\n\n' + table_records(table, number, max_chars, contexts[id(table)]) + '\n\n'))
    return body.get_text('\n', strip=False)


def extract(raw: bytes, media_type: str, charset: str, max_chars: int, max_pages: int) -> tuple[str, str]:
    """Convert one supported document into normalized text and a parser-version label.

    Format libraries extract text; the final cleanup standardizes whitespace and Unicode for
    exact quotation matching later. Limits reject excessive/empty output rather than
    returning a silently truncated document. HTML links are reduced to visible text here;
    html_links() separately captures observed targets.

    Args:
        raw: Original document bytes read by the parent process.
        media_type: Normalized document media type selecting the HTML, PDF or plain-text
            parser.
        charset: Declared text encoding, or None when the parser must use its default.
        max_chars: Maximum permitted number of extracted text characters.
        max_pages: Maximum permitted number of PDF pages.

    Returns:
        A pair of normalized document text ending in a newline and the parser/version label
        used to produce it.
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
        for element in soup.find_all(["script", "style", "nav", "footer", "form", "noscript", "template", "svg", "iframe"]):
            element.decompose()
        for header in soup.find_all('header'):
            # An article's title may live in HEADER. Retain its source headings
            # before removing navigational/header furniture, not a guessed title.
            for heading in list(header.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
                header.insert_before(heading.extract())
            header.decompose()
        body = soup.find("main") or soup.find("article") or soup.body or soup
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        text = structured_html_text(body, max_chars, title)
        parser = "beautifulsoup4-" + bs4.__version__ + "/html.parser/records-v3"
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


def html_source_metadata(soup):
    """Retain explicitly stated document labels without interpreting their truth.

    Args:
        soup: Parsed HTML tree from the bounded parser worker, before cleanup.
    Returns:
        Title and selected metadata declarations, each capped at 1000 characters,
        with at most 32 declarations and an explicit truncation flag. No inferred
        publisher, generated summary or acquisition-derived publication date.
    """
    names = {'publisher', 'dc.publisher', 'dcterms.publisher', 'og:site_name',
        'article:published_time', 'article:modified_time', 'date', 'dc.date',
        'dcterms.created', 'dcterms.modified', 'datepublished', 'datemodified'}
    title = soup.title.get_text(' ', strip=True) if soup.title else None
    declarations, truncated = [], bool(title and len(title) > 1000)
    for tag in soup.find_all('meta'):
        key = str(tag.get('property') or tag.get('name') or '').casefold()
        if key not in names or not tag.get('content'):
            continue
        value = str(tag['content'])
        if len(declarations) >= 32:
            truncated = True
            continue
        truncated = truncated or len(value) > 1000
        declarations.append(dict(name=key, value=value[:1000]))
    return dict(title=title[:1000] if title else None, declarations=declarations,
        truncated=truncated)


def html_links(raw: bytes, media_type: str, charset: str) -> dict:
    """Retain observed anchors before text cleanup removes navigation.

    Targets are raw attributes, not permissions. The parent resolves them against the final
    fetched URL; HTML base tags are deliberately ignored. PDF/text link discovery is not
    implemented. Labels are untrusted hints, not source claims.

    Args:
        raw: Original document bytes; HTML parsing observes anchors without following them.
        media_type: Normalized document media type selecting the HTML, PDF or plain-text
            parser.
        charset: Declared text encoding, or None when the parser must use its default.

    Returns:
        A dictionary of href/label records within the source-byte/parser-resource bounds;
        there is no first-N anchor cutoff. Overlong URLs are excluded and disclosed by
        the truncation flag. Source-stated HTML labels/dates are retained separately
        as metadata, not verified claims. Non-HTML documents produce an empty inventory.
    """
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return {"links": [], "links_truncated": False}
    import bs4
    soup = bs4.BeautifulSoup(raw, "html.parser", from_encoding=charset or None)
    anchors = soup.find_all("a", href=True)
    return {"links": [{"href": str(a["href"])[:2048], "label": a.get_text(" ", strip=True)[:240]}
        for a in anchors if len(str(a["href"])) <= 2048],
        "links_truncated": any(len(str(a["href"])) > 2048 for a in anchors),
        "source_metadata": html_source_metadata(soup)}


def main() -> None:
    """Apply resource limits, parse one input, and never print raw exception details.

    Args:
        None.

    Returns:
        None on normal completion after writing the worker JSON response to stdout; parsing
        failures are represented without raw exception text.
    """
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
