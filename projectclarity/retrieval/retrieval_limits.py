"""Put the retrieval limits and their counters in one easy-to-find place.

For example, a huge download must stop at the file limit, and several smaller
downloads must still fit the whole-run limit. Both local reading and web fetching
charge the same byte counter. Robots requests and redirects count as HTTP calls.
The elapsed-time check stops new work and checks between chunks; OS DNS resolution
is not a hard-deadline operation. Parser subprocesses have their own hard timeout.
The graph passes the operator's validated FetchLimits into these defaults. Optional
callbacks report usage and check cancellation without coupling the counter to a
particular UI. Pacing waits are split so they can observe the same stop request.
"""

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class RetrievalLimits:
    """Conservative development defaults, owned by Python rather than the model."""

    max_source_bytes: int = 5 * 1024 * 1024
    max_total_bytes: int = 25 * 1024 * 1024
    max_http_requests: int = 50
    max_redirects: int = 3
    request_timeout_seconds: float = 10.0
    max_run_seconds: float = 180.0
    min_host_interval_seconds: float = 1.0
    max_robots_wait_seconds: float = 30.0
    max_robots_bytes: int = 512 * 1024
    parser_timeout_seconds: float = 15.0
    max_text_characters: int = 1_000_000
    max_pdf_pages: int = 50


class RetrievalError(Exception):
    """A short, application-written reason safe to save without exception payloads."""

    def __init__(self, code: str, message: str):
        """Keep a machine-readable failure code beside our safe explanation for the user."""
        super().__init__(message)
        self.code = code


class RunCancelled(Exception):
    """A cooperative stop request, not a failed document or permission to retry."""


@dataclass
class RetrievalBudget:
    """One run's shared counters; creating a new run starts new counters."""

    limits: RetrievalLimits = field(default_factory=RetrievalLimits)
    bytes_read: int = 0
    http_requests: int = 0
    started: float = field(default_factory=time.monotonic)
    # Callbacks keep this counter independent of the terminal and log format.
    # The production graph supplies them; isolated budget tests need neither.
    check_control: Callable[[], None] | None = None
    on_event: Callable[..., None] | None = None

    def emit(self, event: str, **details) -> None:
        """Report only application-selected metadata, never response bodies/headers."""
        if self.on_event:
            self.on_event(event, **details, **self.summary())

    def check_time(self) -> None:
        """Observe cancellation and reject further work after the elapsed-time allowance.

        Callers invoke this between operations; it cannot interrupt a blocking
        DNS/HTTP call by itself. Network and parser timeouts provide other bounds.
        """
        if self.check_control:
            self.check_control()
        if time.monotonic() - self.started >= self.limits.max_run_seconds:
            raise RetrievalError("run_time_limit", "Retrieval time allowance exhausted; no further work started.")

    def charge_bytes(self, count: int) -> None:
        """Count bytes already read, then stop if all sources together exceeded the cap.

        The counter includes the last over-limit chunk and robots.txt traffic;
        failed downloads do not get their consumed bytes refunded.
        """
        self.check_time()
        self.bytes_read += count
        self.emit("bytes_read")
        if self.bytes_read > self.limits.max_total_bytes:
            raise RetrievalError("total_size_limit", "The retrieval run exceeded its total byte allowance.")

    def reserve_request(self) -> None:
        """Charge an HTTP attempt before sending it, so failures still consume the limit."""
        self.check_time()
        if self.http_requests >= self.limits.max_http_requests:
            raise RetrievalError("request_limit", "The retrieval HTTP request allowance is exhausted.")
        self.http_requests += 1

    def wait(self, seconds: float) -> None:
        """Honor site pacing while checking stop/deadline requests every quarter second."""
        while seconds > 0:
            self.check_time()
            interval = min(seconds, 0.25)
            time.sleep(interval)
            seconds -= interval
        self.check_time()

    def summary(self) -> dict[str, int | float]:
        """Return counters/limits for receipts and monitoring, without any source content."""
        return {"bytes_read": self.bytes_read, "http_requests": self.http_requests,
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "max_total_bytes": self.limits.max_total_bytes,
                "max_source_bytes": self.limits.max_source_bytes,
                "max_http_requests": self.limits.max_http_requests}
