"""Fetch supplied public web pages while rejecting local/private network targets.

For example, a university page can be downloaded, but a URL pointing at this
computer, a private network, or a cloud metadata service is refused. Every
redirect is checked again. We connect to the checked IP address while still
verifying the original HTTPS hostname; a second DNS answer cannot change the
destination between the check and the connection.

PublicWebReader is used by source_retrieval.py. urllib3 handles HTTP/TLS and Protego
interprets robots.txt, including wildcard rules. Requests are sequential, paced,
bounded, and unauthenticated. No browser, JavaScript, cookies, proxy environment,
automatic retries, link following, or search is involved. Source text is data.
"""

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

import certifi
import urllib3
from protego import Protego

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


def public_target(url: str) -> PublicTarget:
    """Reject unsafe destinations before a socket is opened, including mixed DNS answers."""
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
    """Share robots decisions, rate limits, and byte/request budgets across one run."""

    def __init__(self, budget: RetrievalBudget):
        """Share one fetch allowance and per-origin robots/pacing decisions across sources.

        An origin is a scheme plus host (for example https://example.org).
        These caches last only for this reader/run; no background queue is started.
        """
        self.budget = budget
        self.allowed_origins: set[str] | None = None
        self.policies: dict[str, Protego | RetrievalError] = {}
        self.intervals: dict[str, float] = {}
        self.last_request: dict[str, float] = {}
        self.blocked_origins: set[str] = set()

    def _pace(self, origin: str) -> None:
        """Wait between requests; refuse an excessive requested delay instead of ignoring it."""
        delay = self.intervals.get(origin, self.budget.limits.min_host_interval_seconds)
        if delay > self.budget.limits.max_robots_wait_seconds:
            raise RetrievalError("robots_delay", "The site's requested crawl delay exceeds this run's allowed wait.")
        remaining = self.last_request.get(origin, float("-inf")) + delay - time.monotonic()
        if remaining > 0:
            self.budget.check_time()
            self.budget.emit("rate_limit_wait", stage="waiting", wait_seconds=round(remaining, 3))
            self.budget.wait(remaining)
        self.budget.check_time()

    def _request(self, url: str, max_bytes: int, *, request_kind="document") -> WebResponse:
        """Attempt at most one HTTP request after address, pacing and budget checks.

        Unsafe targets can fail before any request. The caller handles redirects;
        disabling library redirects/retries keeps every attempt visible to our
        access checks and counters. Only a complete bounded HTTP-200 body is read.
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
                               body, response.headers.get("Location"))
        except (urllib3.exceptions.HTTPError, OSError):
            raise RetrievalError("http_failed", "The HTTP connection, timeout, or TLS certificate check failed.") from None
        finally:
            if response is not None:
                response.close()
            pool.close()

    def _check_robots(self, target: PublicTarget) -> None:
        """Fetch robots once per origin and apply its rules to each requested page."""
        if target.origin not in self.policies:
            try:
                robots_url = target.origin + "/robots.txt"
                for redirect in range(self.budget.limits.max_redirects + 1):
                    reply = self._request(robots_url, self.budget.limits.max_robots_bytes, request_kind="robots")
                    if reply.status not in REDIRECT_STATUSES:
                        break
                    if not reply.location or redirect == self.budget.limits.max_redirects:
                        raise RetrievalError("robots_unavailable", "Robots redirect limit reached or target missing.")
                    redirected = public_target(urljoin(robots_url, reply.location))
                    if redirected.origin != target.origin:
                        raise RetrievalError("robots_unavailable", "Cross-origin robots redirects are not supported in this increment.")
                    robots_url = redirected.url
                if reply.status in {404, 410}:
                    rules = ""  # A missing robots file is different from a failed lookup.
                elif reply.status == 200:
                    rules = reply.body.decode("utf-8-sig", errors="replace")
                    if "<html" in rules[:512].lower() or "<!doctype html" in rules[:512].lower():
                        raise RetrievalError("robots_unavailable", "The robots endpoint returned HTML, not a rules file.")
                else:
                    raise RetrievalError("robots_unavailable", f"Robots permission could not be established (HTTP {reply.status}).")
                policy = Protego.parse(rules)
                delay = policy.crawl_delay(USER_AGENT) or 0.0
                rate = policy.request_rate(USER_AGENT)
                if rate:
                    if rate.start_time or rate.end_time:
                        raise RetrievalError("robots_schedule", "Time-windowed robots request rates are not supported.")
                    delay = max(delay, rate.seconds / rate.requests)
                self.intervals[target.origin] = max(delay, self.budget.limits.min_host_interval_seconds)
                self.policies[target.origin] = policy
            except RetrievalError as exc:
                self.policies[target.origin] = exc
        policy = self.policies[target.origin]
        if isinstance(policy, RetrievalError):
            raise policy
        if not policy.can_fetch(target.url, USER_AGENT):
            raise RetrievalError("robots_denied", "robots.txt disallows this source for ProjectClarity.")

    def fetch(self, url: str) -> tuple[WebResponse, str, list[str]]:
        """Follow only bounded HTTP redirects, checking public access and robots at every hop."""
        current = urldefrag(url)[0]
        chain = []
        for redirect in range(self.budget.limits.max_redirects + 1):
            self.budget.check_time()
            self.budget.emit("source_resolving", stage="resolving")
            # Check operator scope before DNS/robots/network activity. Recheck
            # every redirect; following a permitted link grants no new origin.
            if self.allowed_origins is not None:
                from projectclarity.discovery.observed_link_inventory import canonical_url, origin
                normalized = canonical_url(current)
                if not normalized or origin(normalized) not in self.allowed_origins:
                    raise RetrievalError("discovery_origin_denied", "Follow-up target is outside the supplied URL origins.")
            target = public_target(current)
            if target.url in chain:
                raise RetrievalError("redirect_loop", "The source redirects in a loop.")
            chain.append(target.url)
            self.budget.emit("robots_check", stage="checking_robots", host=target.hostname)
            self._check_robots(target)
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
                raise RetrievalError("http_status", f"Source returned HTTP {reply.status}; no content accepted.")
            return reply, target.url, chain
        raise RetrievalError("redirect_limit", "Source redirect limit reached.")
