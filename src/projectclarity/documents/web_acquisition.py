"""Download supplied web addresses and turn their documents into readable text.

The document processor gives this file an address to read; it does not matter
whether that address came from a search, a user or another source. This code checks
website access instructions, downloads permitted content and reads HTML, plain text
and supported PDFs. Page references are retained for PDF evidence.

This file does not run searches or choose what to read next. It reports blocked,
failed and unreadable sources so document processing can show an honest outcome.
It does not run webpage scripts or read text from scanned images.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import http.client
from io import BytesIO
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qs, urlencode, quote

from bs4 import BeautifulSoup
from protego import Protego
from pypdf import PdfReader

AGENT = 'ClarityResearchBot'
USER_AGENT = 'ClarityResearchBot/0.1 (research document reader; honors robots.txt)'


class SourceError(Exception):
    """A classified source failure, distinct from an empty acquired document."""
    def __init__(self, code, message, details=None):
        """Store a safe operational reason for monitoring and receipts.

        Args:
            code: Stable acquisition outcome such as robots_blocked or too_large.
            message: Bounded human-readable explanation, without credentials.
            details: Optional safe diagnostic receipt, such as HTTP status/markers.
        Returns:
            None; initializes exception fields without network activity.
        """
        super().__init__(message)
        self.code = code
        self.details = details


def canonical_url(url):
    """Normalize URL identity without deleting meaningful path/query parameters.

    Args:
        url: Search-result or redirect URL.
    Returns:
        HTTP(S) URL without fragment and default port, or ValueError for credentials,
        unsupported schemes/ports or malformed hosts. Does not resolve DNS.
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Only public HTTP(S) URLs without credentials are supported.')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    if port not in (80, 443):
        raise ValueError('Only standard web ports are supported.')
    host = parts.hostname.encode('idna').decode().lower()
    if ':' in host:
        host = '[' + host + ']'
    netloc = host if port == (443 if parts.scheme == 'https' else 80) else f'{host}:{port}'
    return urlunsplit((parts.scheme, netloc, parts.path or '/', parts.query, ''))


def public_address(url):
    """Resolve a public URL before opening a connection and reject mixed/private DNS.

    Args:
        url: Canonical HTTP(S) URL.
    Returns:
        One validated IP address to pin the connection to. Private, loopback,
        reserved, link-local and mixed answers raise SourceError; DNS failures propagate.
    """
    parts = urlsplit(url)
    addresses = {r[4][0] for r in socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise SourceError('unsafe_url', 'URL resolves to a non-public address.')
    return sorted(addresses)[0]


def http_get(url, limit, cancelled):
    """Fetch one hop using pinned public DNS, verified TLS and bounded bytes/time.

    Args:
        url: URL to fetch, with redirects deliberately not followed here.
        limit: Maximum raw and decompressed response bytes.
        cancelled: Callback that raises if the run was cancelled.
    Returns:
        Status, lowercase response headers and bytes. Raises SourceError on size,
        compression or elapsed limits; network/TLS exceptions propagate. No retry.
    """
    url = canonical_url(url)
    ip = public_address(url)
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    cancelled()
    started = time.monotonic()
    connection = http.client.HTTPConnection(parts.hostname, port, timeout=20)
    try:
        sock = socket.create_connection((ip, port), timeout=20)
        connection.sock = sock
        if parts.scheme == 'https':
            connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        target = urlunsplit(('', '', parts.path or '/', parts.query, ''))
        connection.request('GET', target, headers={'User-Agent':USER_AGENT, 'Accept-Encoding':'identity',
                           'Accept':'text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9,*/*;q=0.1'})
        response = connection.getresponse()
        headers = {k.lower():v for k,v in response.getheaders()}
        chunks, size = [], 0
        while True:
            cancelled()
            if time.monotonic() - started > 60:
                raise SourceError('download_timeout', 'Download exceeded 60 seconds.')
            block = response.read(min(65536, limit + 1 - size))
            if not block:
                break
            size += len(block)
            if size > limit:
                raise SourceError('too_large', f'Document exceeds the {limit:,}-byte limit; no truncated text is extracted.')
            chunks.append(block)
        body = b''.join(chunks)
        encoding = headers.get('content-encoding', '').lower()
        if encoding == 'gzip':
            with gzip.GzipFile(fileobj=BytesIO(body)) as stream:
                body = stream.read(limit + 1)
            if len(body) > limit:
                raise SourceError('too_large', 'Decompressed document exceeds the byte limit.')
        elif encoding not in ('', 'identity'):
            raise SourceError('unsupported_encoding', f'Unsupported content encoding: {encoding}')
        return response.status, headers, body
    finally:
        connection.close()


class RobotsReader:
    """Per-run robots cache, pacing and redirect-aware public document web_acquisition."""
    def __init__(self, root, cancelled, event, transport=http_get):
        """Configure acquisition and callbacks without contacting any origin.

        Args:
            root: Artifact directory for robots receipts and source bytes.
            cancelled: Cancellation check used before requests and during waits.
            event: Callback receiving readable progress messages.
            transport: Single-hop HTTP function; replace only for offline tests.
        Returns:
            None. Initializes per-origin policy cache and last-request times.
        """
        self.root, self.cancelled, self.event, self.transport = root, cancelled, event, transport
        self.policies, self.last = {}, {}
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def retry_wait(self, seconds):
        """Wait cooperatively before a bounded robots retry.

        Args: seconds: Nonnegative delay, at most thirty seconds.
        Returns: None; cancellation interrupts the wait without a further request.
        """
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            self.cancelled();time.sleep(min(.2,max(0,until-time.monotonic())))

    def policy(self, url):
        """Classify robots outcomes rather than treating absence as prohibition.

        Args: url: Document URL whose origin defines the policy scope.
        Returns: Parsed rules for successful 2xx, None for unavailable 4xx (except
            temporary 408/429), or raises for unreachable/unsafe outcomes. At most
            three attempts, each with five redirects. 202 gets bounded retries,
            then its successful response is parsed, including any usable rules.
            Retry-After is respected; waits over thirty seconds defer this origin.
            Outcomes and attempts are retained and cached for the current run.
        """
        parts=urlsplit(url);origin=f'{parts.scheme}://{parts.netloc}'
        if origin in self.policies:
            cached=self.policies[origin]
            if isinstance(cached,SourceError):raise cached
            return cached
        robots_url=origin+'/robots.txt'
        receipt={'origin':origin,'user_agent':USER_AGENT,'hops':[],'policy_version':2}
        filename=hashlib.sha256(origin.encode()).hexdigest()
        try:
            for attempt in range(3):
                current=robots_url;headers={};status=None
                try:
                    for hop in range(6):
                        self.cancelled();self.event(f'Checking robots.txt (attempt {attempt+1}/3): '+current)
                        status,headers,body=self.transport(current,512000,self.cancelled)
                        receipt['hops'].append({'attempt':attempt+1,'url':current,'status':status,'bytes':len(body)})
                        self.last[origin]=time.monotonic()
                        if status in (301,302,303,307,308):
                            if hop==5:
                                receipt['decision']='unavailable_redirect_limit';self.policies[origin]=None;return None
                            current=canonical_url(urljoin(current,headers.get('location','')));continue
                        break
                except (OSError,http.client.HTTPException) as error:
                    status=None
                    receipt['hops'].append({'attempt':attempt+1,'url':current,'error':type(error).__name__})
                temporary=status is None or status in (202,408,429) or status>=500
                if temporary and attempt<2:
                    delay=2*(attempt+1);value=headers.get('retry-after')
                    if value:
                        try:delay=max(delay,float(value))
                        except ValueError:
                            try:delay=max(delay,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                            except (ValueError,TypeError,OverflowError):pass
                    if delay>30:raise SourceError('robots_deferred','robots.txt requested a longer retry delay; deferred, not explicitly disallowed.')
                    receipt.setdefault('retry_delays',[]).append(delay)
                    self.event(f'Robots check temporarily incomplete; retrying in {delay:g} seconds.')
                    self.retry_wait(delay);continue
                if status is not None and 200<=status<300:
                    text=body.decode('utf-8-sig',errors='replace');parser=Protego.parse(text)
                    receipt.update(decision='parsed_success',text=text,status=status)
                    self.policies[origin]=parser;return parser
                if status is not None and 400<=status<500 and status not in (408,429):
                    receipt.update(decision='unavailable_no_rules',status=status)
                    self.event(f'robots.txt HTTP {status}: no available rules; proceeding with normal pacing.')
                    self.policies[origin]=None;return None
                raise SourceError('robots_unreachable',f'Could not obtain robots rules after bounded attempts (HTTP {status}); deferred, not explicitly disallowed.')
        except Exception as error:
            if isinstance(error,InterruptedError):
                receipt['decision']='cancelled';raise
            failure=error if isinstance(error,SourceError) else SourceError('robots_unreachable','Could not retrieve robots.txt: '+type(error).__name__)
            receipt.update(decision='deferred',error=str(failure));self.policies[origin]=failure;raise failure
        finally:
            path=self.root/f'robots-{filename}.json'
            path.write_text(json.dumps(receipt,indent=2));path.chmod(0o600)

    def allowed(self, url):
        """Enforce rules and respectful pacing before each document or redirect hop.

        Args:
            url: Public target URL to check, including its path and query.
        Returns:
            None after waiting; disallowed/unsupported pacing raises SourceError.
            Uses named-agent rules, Allow precedence and wildcards through Protego.
        """
        parser = self.policy(url)
        if parser is not None and not parser.can_fetch(url, AGENT):
            raise SourceError('robots_blocked', 'robots.txt disallows this URL for '+AGENT+'.')
        delay = 1.0
        if parser is not None:
            delay = max(delay, float(parser.crawl_delay(AGENT) or 0))
            rate = parser.request_rate(AGENT)
            if rate:
                delay = max(delay, float(rate.seconds) / rate.requests)
        if delay > 60:
            raise SourceError('robots_delay', 'Site requests a crawl delay above 60 seconds; skipped rather than ignoring it.')
        parts = urlsplit(url);origin = f'{parts.scheme}://{parts.netloc}'
        until = self.last.get(origin, 0) + delay
        while time.monotonic() < until:
            self.cancelled();time.sleep(min(.2, until - time.monotonic()))
        self.last[origin] = time.monotonic()

    def fetch(self, url):
        """Download an allowed source, rechecking robots for every redirect target.

        Args:
            url: Discovered public document URL.
        Returns:
            Final URL, response headers, complete bounded body and redirect history.
            A robots denial means no GET of that target. HTTP failures, challenges
            and excessive redirects are explicit failures, never empty documents.
        """
        current, hops = canonical_url(url), []
        for _ in range(6):
            self.allowed(current)
            status, headers, body = self.transport(current, 3_000_000, self.cancelled)
            hops.append({'url':current,'status':status,'bytes':len(body)})
            if status in (301,302,303,307,308):
                if not headers.get('location'):
                    raise SourceError('bad_redirect','Redirect has no destination.')
                current = canonical_url(urljoin(current, headers['location']))
                continue
            if status != 200:
                raise SourceError('http_error',f'Source returned HTTP {status}; no retry or access bypass.')
            lower = body[:20000].lower()
            if b'cf-chl-' in lower or b'captcha-container' in lower:
                raise SourceError('access_challenge','Source presented an access challenge; not bypassed.')
            return current, headers, body, hops
        raise SourceError('redirect_limit','Source exceeded five redirects.')


def parse_document(body, content_type):
    """Parse source bytes independently of acquisition, preserving PDF page offsets.

    Args:
        body: Complete document bytes from a download or a future local reader.
        content_type: MIME hint; a PDF signature takes precedence.
    Returns:
        Text, parser, page spans and warnings. Page numbers are one-based physical
        PDF pages, not printed labels. Empty pages remain recorded; mixed scanned
        pages are flagged, not silently claimed as read. No OCR or table recovery.
        Invalid/encrypted, empty or over-100-page PDFs raise classified SourceError.
    """
    pages, warnings = [], []
    if body.startswith(b'%PDF-') or 'application/pdf' in content_type:
        try:
            reader = PdfReader(BytesIO(body))
            if reader.is_encrypted and not reader.decrypt(''):
                raise SourceError('encrypted_pdf', 'PDF requires a password; no text extracted.')
            if len(reader.pages) > 100:
                raise SourceError('too_large', 'PDF exceeds 100 pages; no partial extraction.')
            parts, offset = [], 0
            for number, page in enumerate(reader.pages, 1):
                content = page.extract_text() or ''
                if parts:
                    parts.append('\n\n'); offset += 2
                pages.append({'page':number, 'start':offset, 'end':offset+len(content),
                              'has_text':bool(content.strip())})
                parts.append(content); offset += len(content)
            text = ''.join(parts)
        except SourceError:
            raise
        except Exception as error:
            raise SourceError('invalid_pdf', 'PDF could not be parsed: '+type(error).__name__) from error
        missing = [p['page'] for p in pages if not p['has_text']]
        if missing:
            warnings.append('Pages without readable text (blank or scanned; OCR unavailable): '+', '.join(map(str,missing)))
        parser = 'pypdf'
    elif 'html' in content_type or 'text/plain' in content_type or not content_type:
        soup = BeautifulSoup(body, 'html.parser')
        for tag in soup(['script','style','noscript','template','svg']):
            tag.decompose()
        text = soup.get_text('\n', strip=True) if 'text/plain' not in content_type else body.decode('utf-8',errors='replace')
        parser = 'beautifulsoup' if 'text/plain' not in content_type else 'plain_utf8'
    else:
        raise SourceError('unsupported_type','Unsupported content type: '+content_type[:120])
    if not text.strip():
        raise SourceError('empty_text', 'No readable text was found (scanned PDFs need OCR).',
                          {'pages':pages,'warnings':warnings})
    return {'text':text, 'parser':parser, 'pages':pages, 'warnings':warnings}


def document_text(body, content_type):
    """Keep the existing text-only parser interface for standalone callers.

    Args:
        body: Complete source bytes, never a file path.
        content_type: MIME hint passed to the shared parser.
    Returns:
        Complete text and parser label; propagates classified parsing failures.
        Use parse_document when page provenance and warnings are needed.
    """
    parsed = parse_document(body, content_type)
    return parsed['text'], parsed['parser']




def read_document_text(path):
    """Read saved source text without changing line endings or evidence offsets.

    Args: path: Path to a private UTF-8 document.txt file, not a network URL.
    Returns: Exact saved text, including CRLF sequences. Ordinary universal-newline
        reads can change hashes and invalidate extraction spans. File and decoding
        errors propagate; this function performs no repair or normalization.
    """
    with path.open(encoding='utf-8', newline='') as stream:
        return stream.read()


def split_text(text, limit=3000):
    """Partition complete document text without dropping characters or overflowing chunks.

    Args:
        text: Saved parsed document text.
        limit: Positive character bound, normally 3,000.
    Returns:
        Ordered exact substrings whose concatenation equals the source. Prefers
        paragraph/line boundaries; long single lines split at spaces or hard limit.
    """
    if limit < 1:
        raise ValueError('Chunk limit must be positive')
    chunks=[]
    while text:
        end = min(limit,len(text))
        if end < len(text):
            boundary = text.rfind('\n',limit//2,end)
            if boundary < 0:
                boundary = text.rfind(' ',limit//2,end)
            if boundary >= 0:
                end = boundary+1
        chunks.append(text[:end]);text=text[end:]
    return chunks









