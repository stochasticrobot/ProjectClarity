"""Fetch registered public web pages while rejecting local/private network targets.

For example, a university page can be downloaded, but a URL pointing at this
computer, a private network, or a cloud metadata service is refused. Every
redirect is checked again. We connect to the checked IP address while still
verifying the original HTTPS hostname; a second DNS answer cannot change the
destination between the check and the connection.

PublicWebReader is used by source_retrieval.py. urllib3 handles HTTP/TLS.
Robots.txt is not requested or interpreted; it cannot deny or delay retrieval.
Requests are sequential, paced by application policy,
bounded, and unauthenticated. No browser, JavaScript, cookies, proxy environment,
automatic retries, page-link selection, or search is involved. The caller may
supply an authorized discovered URL; this fetcher still applies the same access
checks. HTTP redirects are followed only within the configured limits and scope.
The workflow assigns the request's domain-policy snapshot to the reader. Denies
and optional whitelist-only checks run before DNS for sources and redirects.
Optional seed-origin confinement applies to followed sources, not to every seed.
"""

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

import certifi
import urllib3

from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalError


USER_AGENT = "ProjectClarity/0.1"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class PublicTarget:
    """One validated URL and the specific public IP selected for its connection."""

    url: str
    origin: str
    hostname: str
    port: int
    ip: str
    request_path: str


@dataclass(frozen=True)
class WebResponse:
    """The complete bounded response, without cookies or arbitrary response headers."""

    status: int
    content_type: str
    body: bytes
    location: str | None = None
    retry_after_seconds: float | None = None


def retry_delay(value):
    """Parse Retry-After without shortening a server's requested cooldown.

    Args:
        value: HTTP Retry-After header text or None; seconds and HTTP dates work.

    Returns:
        Nonnegative delay in seconds, or None for invalid/absent headers. This
        does not authorize retries of denied, rate-limited or certificate errors.
    """
    from datetime import UTC, datetime
    from email.utils import parsedate_to_datetime
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return min(float(value), 315360000.0)
        date = parsedate_to_datetime(value)
        return max(0, (date - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def public_target(url: str) -> PublicTarget:
    """Reject unsafe destinations before a socket is opened, including mixed DNS answers.

    Args:
        url: HTTP(S) address of the page to inspect or retrieve.

    Returns:
        A PublicTarget containing the normalized address, origin, host, port, request path
        and vetted IP to pin for connection; unsafe targets raise RetrievalError.
    """
    try:
        parts = urlsplit(url)
        if (len(url) > 2048 or any(char.isspace() or not char.isprintable() for char in url)
                or parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port != (443 if parts.scheme == "https" else 80) or "%" in hostname:
            raise ValueError
        if hostname.rstrip(".") == "localhost" or hostname.rstrip(".").endswith((".localhost", ".local", ".internal")):
            raise ValueError
    except (ValueError, UnicodeError):
        raise RetrievalError("blocked_url", "Only public HTTP(S) URLs on their standard ports are allowed.") from None
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}
    except OSError:
        raise RetrievalError("dns_failed", "The source hostname could not be resolved.") from None
    if not addresses:
        raise RetrievalError("dns_failed", "The source hostname returned no addresses.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # Also refuse IPv6 transition encodings rather than allowing them to hide
        # a private IPv4 destination. All DNS answers must be public, not just one.
        transitional = ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo
                                            or ip in ipaddress.ip_network("64:ff9b::/96"))
        if not ip.is_global or ip.is_multicast or transitional:
            raise RetrievalError("blocked_address", "The source resolves to a non-public or prohibited network address.")
    address = sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))[0]
    authority = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"{parts.scheme}://{authority}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    normalized = urlunsplit((parts.scheme, authority, path, query, ""))
    return PublicTarget(normalized, origin, hostname, port, address, path + ("?" + query if query else ""))


class PublicWebReader:
    """Share application pacing, HTTP rate-limit state and retrieval budgets across a run."""

    def __init__(self, budget: RetrievalBudget):
        """Share one fetch allowance and application-owned pacing across sources.

        An origin is a scheme plus host (for example https://example.org). These caches last
        only for this reader/run; no background queue is started.
        The constructor does not load policy files. The workflow sets url_policy
        from the request and allowed_origins for each source; None leaves that
        optional layer unrestricted, not the underlying public-address checks.

        Args:
            self: Web reader holding this invocation's shared retrieval budget and per-origin
                pacing caches.
            budget: Shared RetrievalBudget charging bytes, HTTP attempts and elapsed time across
                this run's sources.

        Returns:
            None. Initialize shared counters, per-origin pacing caches without
            making a request.
        """
        self.budget = budget
        self.allowed_origins: set[str] | None = None
        self.url_policy = None
        self.last_request: dict[str, float] = {}
        self.blocked_origins: set[str] = set()

    def _pace(self, origin: str) -> None:
        """Wait between requests using only the application-owned host interval.

        Args:
            self: Web reader holding this invocation's shared retrieval budget and per-origin
                pacing caches.
            origin: Normalized scheme/host/port key whose last-request time is checked.

        Returns:
            None after any required application delay; raises on exhausted time or
            cancellation. No site-declared crawl-delay or robots policy is consulted.
        """
        delay = self.budget.limits.min_host_interval_seconds
        remaining = self.last_request.get(origin, float("-inf")) + delay - time.monotonic()
        if remaining > 0:
            self.budget.check_time()
            self.budget.emit("rate_limit_wait", stage="waiting", wait_seconds=round(remaining, 3))
            self.budget.wait(remaining)
        self.budget.check_time()

    def _request(self, url: str, max_bytes: int, *, request_kind="document") -> WebResponse:
        """Attempt at most one HTTP request after address, pacing and budget checks.

        Unsafe targets can fail before any request. The caller handles redirects; disabling
        library redirects/retries keeps every attempt visible to our access checks and counters.
        Only a complete bounded HTTP-200 body is read.

        Args:
            self: Web reader holding this invocation's shared retrieval budget and per-origin
                pacing caches.
            url: HTTP(S) address of the page to inspect or retrieve.
            max_bytes: Maximum response-body bytes permitted for this particular request.
            request_kind: Operational request label, normally document; does not enable
                any policy fetch or additional network request.

        Returns:
            A WebResponse containing status, content type, bounded body bytes and redirect
            location; access, network or budget failures raise RetrievalError.
        """
        self.budget.check_time()
        self.budget.emit("resolving", stage="resolving", request_kind=request_kind)
        target = public_target(url)
        if target.origin in self.blocked_origins:
            raise RetrievalError("rate_limited", "This origin returned HTTP 429; no further requests are made this run.")
        self._pace(target.origin)
        self.budget.reserve_request()
        started = time.monotonic()
        self.budget.emit("request_started", stage="downloading", host=target.hostname, request_kind=request_kind)
        self.last_request[target.origin] = time.monotonic()
        options = {"port": target.port, "timeout": urllib3.Timeout(
            connect=self.budget.limits.request_timeout_seconds, read=self.budget.limits.request_timeout_seconds),
            "retries": False}
        if target.url.startswith("https:"):
            pool = urllib3.HTTPSConnectionPool(target.ip, server_hostname=target.hostname,
                assert_hostname=target.hostname, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), **options)
        else:
            pool = urllib3.HTTPConnectionPool(target.ip, **options)
        response = None
        try:
            response = pool.request("GET", target.request_path, headers={
                "Host": urlsplit(target.origin).netloc, "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity", "Accept": "text/html, text/plain, application/pdf, */*;q=0.1"},
                preload_content=False, decode_content=False, redirect=False, retries=False, assert_same_host=False)
            self.budget.emit("http_response", http_status=response.status, request_kind=request_kind)
            if response.status == 429:
                self.blocked_origins.add(target.origin)
            body = b""
            # Do not download error/redirect bodies. Successful bodies are read
            # in chunks; a false Content-Length cannot bypass the real byte cap.
            if response.status == 200:
                if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                    raise RetrievalError("encoded_response", "Server ignored the uncompressed-response request; compressed bodies are not supported yet.")
                length = response.headers.get("Content-Length")
                if length and (not length.isdigit() or int(length) > max_bytes):
                    raise RetrievalError("source_size_limit", "The source exceeds its byte limit or declares an invalid length.")
                chunks = []
                size = 0
                while True:
                    self.budget.check_time()
                    chunk = response.read(min(65536, max_bytes - size + 1), decode_content=False)
                    if not chunk:
                        break
                    self.budget.charge_bytes(len(chunk))
                    size += len(chunk)
                    if size > max_bytes:
                        raise RetrievalError("source_size_limit", "The source exceeds its byte limit.")
                    chunks.append(chunk)
                body = b"".join(chunks)
            self.budget.emit("request_finished", http_status=response.status, request_kind=request_kind,
                             request_seconds=round(time.monotonic() - started, 3))
            return WebResponse(response.status, response.headers.get("Content-Type", "application/octet-stream"),
                               body, response.headers.get("Location"), retry_delay(response.headers.get("Retry-After")))
        except urllib3.exceptions.SSLError:
            raise RetrievalError('tls_failed', 'TLS certificate validation failed; no automatic retry.') from None
        except urllib3.exceptions.TimeoutError:
            raise RetrievalError('http_timeout', 'The HTTP request timed out.') from None
        except (urllib3.exceptions.NewConnectionError, urllib3.exceptions.ProtocolError):
            raise RetrievalError('connection_failed', 'The HTTP connection could not complete.') from None
        except (urllib3.exceptions.HTTPError, OSError):
            raise RetrievalError("http_failed", "The HTTP connection, timeout, or TLS certificate check failed.") from None
        finally:
            if response is not None:
                response.close()
            pool.close()

    def fetch(self, url: str) -> tuple[WebResponse, str, list[str]]:
        """Follow only bounded HTTP redirects, checking public-network and domain rules at every hop.

        Frozen domain rules and optional seed-origin confinement apply before DNS,
        including each redirect target, so an allowed starting URL cannot bypass policy.

        Args:
            self: Web reader holding this invocation's shared retrieval budget and per-origin
                pacing caches.
            url: HTTP(S) address of the page to inspect or retrieve.

        Returns:
            A tuple of the final WebResponse, final URL and redirect-chain records; disallowed
            or unsuccessful retrieval raises RetrievalError.
        """
        current = urldefrag(url)[0]
        chain = []
        for redirect in range(self.budget.limits.max_redirects + 1):
            self.budget.check_time()
            self.budget.emit("source_resolving", stage="resolving")
            if self.url_policy is not None:
                from projectclarity.discovery.url_policy import url_denial
                if reason := url_denial(current, self.url_policy):
                    raise RetrievalError(reason, "URL denied by the configured domain policy.")
            # Check operator scope before DNS/network activity. Recheck
            # every redirect. When origin confinement is enabled, a permitted
            # starting link does not authorize a redirect outside that scope.
            if self.allowed_origins is not None:
                from projectclarity.discovery.observed_link_inventory import canonical_url, origin
                normalized = canonical_url(current)
                if not normalized or origin(normalized) not in self.allowed_origins:
                    raise RetrievalError("discovery_origin_denied", "Follow-up target is outside the supplied URL origins.")
            target = public_target(current)
            if target.url in chain:
                raise RetrievalError("redirect_loop", "The source redirects in a loop.")
            chain.append(target.url)
            reply = self._request(target.url, self.budget.limits.max_source_bytes)
            if reply.status in REDIRECT_STATUSES:
                if redirect == self.budget.limits.max_redirects or not reply.location:
                    raise RetrievalError("redirect_limit", "Source redirect limit reached or target missing.")
                current = urljoin(target.url, reply.location)
                if target.url.startswith("https:") and not current.startswith("https:"):
                    raise RetrievalError("redirect_downgrade", "An HTTPS source tried to redirect to an insecure connection.")
                self.budget.emit("redirect", http_status=reply.status)
                continue
            if reply.status != 200:
                code = f'http_{reply.status}' if reply.status in {502, 503, 504} else 'http_status'
                raise RetrievalError(code, f"Source returned HTTP {reply.status}; no content accepted.",
                    retry_after_seconds=reply.retry_after_seconds, http_status=reply.status)
            return reply, target.url, chain
        raise RetrievalError("redirect_limit", "Source redirect limit reached.")
